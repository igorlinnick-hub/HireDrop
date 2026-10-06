// Two Indeed smartapply hand-backs from one live run (2026-10-06, ext 1.8.43). Run:
//
//   node <repo>/jobflow/chrome-extension/tests/indeed-eeo-and-length.test.js
//
// 1. demographic-questions: STEP 3 filled=[radio×1,combo×1], STEP 4 filled=[], then
//    "Review your application" refused 3× — alerts=["Choose an option to continue."],
//    invalid/reqEmpty=["Veteran Status We're an equal opportunity employer You are requested
//    (not required…"]. The policy is to DECLINE self-identification, never to invent it, so a
//    required EEO group with a decline option must get that option. The decline was invisible:
//    four copies of one pattern matched "don't" only with an ASCII apostrophe (Indeed and
//    Greenhouse print U+2019 "don’t" — see fixtures/gh-doordash-form.html) and never matched
//    "choose not to self-identify". The radio filler also read an Indeed Mosaic radiogroup's
//    question from getFieldLabel(fieldset) = the first OPTION's <label> (the question is on
//    aria-labelledby — fixtures/indeed-resume-selection.html), and an option whose input sits
//    in an empty indicator <span> read as "".
//
//    ⚠ NO CAPTURED demographic-questions PAGE EXISTS in fixtures/. The markup below is modelled
//    on the captured Indeed Mosaic radio-card group (indeed-resume-selection.html: fieldset
//    role=radiogroup + aria-labelledby, input id "_r_N_-input", label[for] > span) with the
//    question text from the live hand-back diag. The option wording is the OFCCP/Greenhouse
//    one, NOT copied from Indeed. It needs a live capture to replace it.
//
// 2. questions-module: STEP 6 filled=[text×3], then Continue refused 3× — alerts=["Answer must
//    be shorter than 100 characters."] invalid=["Duties/Responsibilities","If no, why not?"].
//    The fields carry no maxlength; the limit exists only in the page's message after Continue.
//    Our AI answers (1-3 sentences) are now cut to fit on a sentence/clause/word boundary and
//    re-entered once; facts, URLs and numbers are never truncated. Synthetic DOM (field shape
//    from the diag), no captured page.

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

function extract(signature) {
  const at = SRC.indexOf(signature);
  if (at < 0) return null;
  const open = SRC.indexOf("{", at + signature.length - 1);
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
const need = (sig) => {
  const f = extract(sig);
  check(`found: ${sig.trim().slice(0, 60)}`, !!f);
  return f || "";
};

function world(html) {
  const dom = new JSDOM(`<!doctype html><body><main>${html}</main></body>`, { runScripts: "outside-only" });
  const w = dom.window;
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", {
    get() { return this.closest("[hidden]") ? null : this.parentElement; },
  });
  w.CSS = { escape: (s) => String(s).replace(/["\\\]\[#.:]/g, "\\$&") };
  w.sleep = async () => {};
  w.humanDelay = () => 0;
  w.formScope = () => w.document;
  w.__logs = [];
  w.logBackend = (t) => w.__logs.push(t);
  w.log = (t) => w.__logs.push(t);
  return w;
}

(async () => {
  // ---- 1a. The decline matcher ----------------------------------------------------------
  const isDecline = need("  function isDeclineOption(text) {");
  const isDemo = need("  function isDemographicQuestion(label, optionTexts) {");
  const pure = {};
  vm.createContext(pure);
  try { vm.runInContext(`${isDecline}\n${isDemo}\nglobalThis.D = { isDeclineOption, isDemographicQuestion };`, pure); } catch (e) { check("decline matcher loads", false, e.message); }
  const { isDeclineOption, isDemographicQuestion } = pure.D || {};
  const DECLINES = [
    "I don’t wish to answer",            // U+2019 — the shape Indeed/Greenhouse print
    "I don't wish to answer",
    "I choose not to self-identify",
    "Decline to self identify",
    "I do not want to answer",
    "Prefer not to say",
    "I wish not to disclose",
  ];
  const CLAIMS = [
    "I am not a protected veteran",
    "I identify as one or more of the classifications of protected veteran",
    "No, I do not identify as transgender",
    "No, I don’t have a disability",
    "Male",
    "I don’t want to relocate",
  ];
  for (const t of DECLINES) check(`decline: “${t}”`, isDeclineOption && isDeclineOption(t));
  for (const t of CLAIMS) check(`not a decline: “${t}”`, isDeclineOption && !isDeclineOption(t));
  check("the glued Indeed label still reads as a veteran question",
    isDemographicQuestion && isDemographicQuestion("Veteran StatusWe're an equal opportunity employerYou are requested (not required) to answer", []));
  check("“embrace” / “Essex” are not EEO questions",
    isDemographicQuestion && !isDemographicQuestion("Describe a time you embraced change", ["Yes", "No"]) &&
    !isDemographicQuestion("Are you willing to work in Essex?", ["Yes", "No"]));

  // ---- 1b. The radio filler on an Indeed-Mosaic-shaped demographic step ------------------
  const radioParts = [
    need("  function getFieldLabel(el) {"),
    isDecline, isDemo,
    extract("  function radioGroupQuestion(g) {") || "",
    extract("  function ownWrappingLabel(o) {") || "",
    need("  async function fillRadioQuestions() {"),
  ];
  // Mosaic radio card, as captured on resume-selection: input#_r_N_-input + label[for] > span.
  const card = (id, name, text) => `
    <div data-testid="radio-card" id="_r_${id}_"><input id="_r_${id}_-input" type="radio" name="${name}" value="${id}">
      <span><span><span></span><span><label for="_r_${id}_-input"><span style="white-space: normal;">${text}</span></label></span></span></span></div>`;
  // Same group with the input inside an empty indicator span and a wrapping label (no for=).
  const wrapped = (name, text) => `
    <label><span class="indicator"><input type="radio" name="${name}"></span><span>${text}</span></label>`;
  const STEP = `
    <form>
      <div id="vet-q"><h2>Veteran Status</h2><p>We're an equal opportunity employer</p><p>You are requested (not required) to answer the questions below.</p></div>
      <fieldset role="radiogroup" aria-labelledby="vet-q" aria-required="true">
        ${card(1, "veteran", "I identify as one or more of the classifications of protected veteran")}
        ${card(2, "veteran", "I am not a protected veteran")}
        ${card(3, "veteran", "I don’t wish to answer")}
      </fieldset>
      <div id="gender-q">Gender</div>
      <fieldset role="radiogroup" aria-labelledby="gender-q">
        ${wrapped("gender", "Male")}${wrapped("gender", "Female")}${wrapped("gender", "I don’t wish to answer")}
      </fieldset>
    </form>`;
  {
    const w = world(STEP);
    w.storageGet = async () => ({ profile: {}, currentJobInfo: {} });
    w.answerWorkStatus = async () => null;
    w.isConsentToProcess = () => false;
    w.isPersonalKnockout = () => false;
    w.pickOptionDeterministic = () => null;
    w.humanClick = async (el) => { (el.control || el).click(); };
    w.eval(`${radioParts.join("\n")}\nwindow.__radio = fillRadioQuestions;`);
    let n = 0;
    try { n = await w.__radio(); } catch (e) { check("fillRadioQuestions ran", false, e.message); }
    const picked = (name) => {
      const r = w.document.querySelector(`input[name="${name}"]:checked`);
      if (!r) return null;
      return (w.document.querySelector(`label[for="${r.id}"]`) || r.closest("label")).textContent.trim();
    };
    check("Veteran Status (Mosaic card, curly apostrophe) → the decline option",
      picked("veteran") === "I don’t wish to answer", String(picked("veteran")));
    check("Gender (input in an empty indicator span) → the decline, never “Male”",
      picked("gender") === "I don’t wish to answer", String(picked("gender")));
    check("…both groups counted", n === 2, `filled=${n}`);
  }
  {
    // No decline offered → blank (hand-back), never an identity value of ours.
    const w = world(`
      <div id="q">Are you a protected veteran?</div>
      <fieldset role="radiogroup" aria-labelledby="q">${card(7, "v2", "Yes")}${card(8, "v2", "No")}</fieldset>`);
    w.storageGet = async () => ({ profile: {}, currentJobInfo: {} });
    w.answerWorkStatus = async () => null;
    w.isConsentToProcess = () => false;
    w.isPersonalKnockout = () => false;
    w.pickOptionDeterministic = () => null;
    w.humanClick = async (el) => { (el.control || el).click(); };
    w.eval(`${radioParts.join("\n")}\nwindow.__radio = fillRadioQuestions;`);
    await w.__radio();
    check("a veteran yes/no with no decline stays blank (question read from aria-labelledby, not “Yes”)",
      !w.document.querySelector("input:checked"));
  }

  // ---- 1c. Dropdowns: the decline, or nothing — never the model ---------------------------
  {
    const payStart = SRC.indexOf("  const PAY_SRC =");
    const payEnd = SRC.indexOf("  // Demographic / EEO self-identification");
    const wsBlock = SRC.slice(SRC.indexOf("  // ── Legal work status: ONE reading"), SRC.indexOf("  // Pick a dropdown option deterministically"));
    const asked = [];
    const ctx = { _aiAnswersUsed: 0, MAX_AI_ANSWERS_PER_FORM: 15, _aiBudgetNotified: false, log() {}, logBackend() {},
      sendMsg: async (m) => { asked.push(m.data.question); return { answer: "I am not a protected veteran" }; } };
    vm.createContext(ctx);
    vm.runInContext(`${SRC.slice(payStart, payEnd)}\n${isDemo}\n${isDecline}\n${wsBlock}\n` +
      `${extract("  function pickOptionDeterministic(label, options, profile) {")}\n` +
      `${extract("  async function chooseOption(label, options, profile, jobInfo) {")}\nglobalThis.choose = chooseOption;`, ctx);
    const vet = ["I identify as one or more of the classifications of protected veteran", "I am not a protected veteran",
      "I choose not to self-identify"].map((text) => ({ text }));
    const got = await ctx.choose("Veteran Status", vet, {}, {});
    check("veteran dropdown → “I choose not to self-identify”", got?.text === "I choose not to self-identify", got?.text);
    const none = await ctx.choose("Are you a protected veteran?", [{ text: "Yes" }, { text: "No" }], {}, {});
    check("veteran dropdown with no decline → nothing, and the model is never asked",
      none === null && asked.length === 0, `${none?.text} asked=${asked.length}`);
  }

  // ---- 2a. Length limits from the page's own words ---------------------------------------
  const lenParts = ["  function charLimitFromText(text) {", "  function fieldCharLimit(el, pageAlertText) {",
    "  function shortenAnswer(text, limit) {", "  async function shortenRefusedAnswers(scope) {"].map(need);
  const L = {};
  vm.createContext(L);
  try { vm.runInContext(`${lenParts[0]}\n${lenParts[2]}\nglobalThis.T = { charLimitFromText, shortenAnswer };`, L); } catch (e) { check("length helpers load", false, e.message); }
  const { charLimitFromText, shortenAnswer } = L.T || {};
  if (charLimitFromText) {
    check("“shorter than 100 characters” → 99 (strict)", charLimitFromText("Answer must be shorter than 100 characters.") === 99);
    check("“Maximum 250 characters” → 250", charLimitFromText("Maximum 250 characters") === 250);
    check("“1,000 characters max” → 1000", charLimitFromText("1,000 characters max") === 1000);
    check("a counter (“45 characters remaining”) is not a limit", charLimitFromText("45 characters remaining") === null);
  }
  const DUTIES = "Managed the social media calendar across four channels, wrote campaign copy, and reported weekly engagement to leadership. I also coordinated with designers on creative assets.";
  const WHY_NOT = "No. I left because the company closed its Honolulu office and moved all marketing roles to the mainland headquarters.";
  if (shortenAnswer) {
    const words = (s) => s.replace(/[.,;:]/g, "").split(/\s+/);
    const ARMY = "I am a U.S. citizen and served in the U.S. Army for four years. I can start immediately after a two week notice.";
    for (const [text, lim] of [[DUTIES, 99], [WHY_NOT, 99], [ARMY, 60]]) {
      const out = shortenAnswer(text, lim);
      const src = words(text);
      check(`shortened to ≤${lim}, whole words in order, ends a sentence: “${out}”`,
        out.length > 0 && out.length <= lim && /[.!?]$/.test(out) && !/…|\.\.\./.test(out) &&
        words(out).every((wd, i) => wd === src[i]), `${out.length}: ${out}`);
    }
    check("the reason survives a short first sentence (“No.” alone is not an answer)", shortenAnswer(WHY_NOT, 99).length > 40);
    check("a word cut never strands a quantity (“…for four.” without “years”)", !/\bfour\.$/.test(shortenAnswer(ARMY, 60)), shortenAnswer(ARMY, 60));
    check("…nor flips a negation (“I did not” → never “I did.”)",
      !/\bdid\.$/.test(shortenAnswer("Looking back over the whole period I did not relocate because my family stayed in Honolulu.", 40)),
      shortenAnswer("Looking back over the whole period I did not relocate because my family stayed in Honolulu.", 40));
    check("an answer that already fits is untouched", shortenAnswer("Yes, I can.", 99) === "Yes, I can.");
    check("a URL is never truncated", shortenAnswer("https://www.linkedin.com/in/" + "x".repeat(120), 99) === "");
    check("a number is never truncated", shortenAnswer("1234567890 ".repeat(12).trim(), 99) === "");
  }

  // ---- 2b. The live loop: fill → page refuses for length → shortened once → accepted -----
  const textParts = [
    need("  function getFieldLabel(el) {"), need("  function setNativeValue(el, value) {"),
    need("  function quickSet(el, value) {"), ...lenParts, need("  async function fillTextQuestions() {"),
  ];
  function textWorld(html, answers) {
    const w = world(html);
    w.storageGet = async () => ({ profile: { name: "Ann", email: "a@b.co" }, currentJobInfo: { title: "Marketing Coordinator" } });
    w.localDay = () => "2026-10-06";
    w.reactSelectShownValue = () => "";
    w.isReactSelectField = () => false;
    w.SCHOOL_FIELD_RE = /$^/; w.NAME_I18N_RE = /$^/; w.NAME_I18N_LAST_RE = /$^/; w.NAME_I18N_FIRST_RE = /$^/;
    w.EMAIL_I18N_RE = /$^/; w.PHONE_I18N_RE = /$^/; w.LETTER_LABEL_RE = /$^/;
    w.workStatus = () => null;
    w.payQuestion = () => null;
    w.__asked = [];
    w.sendMsg = async (m) => { w.__asked.push(m.data.question); return { answer: answers[m.data.question] || "" }; };
    w.eval(`let _aiAnswersUsed = 0; let _aiBudgetNotified = false; const MAX_AI_ANSWERS_PER_FORM = 15;
      const _ourTextAnswers = new Set();\n${textParts.join("\n")}\nwindow.__text = fillTextQuestions;`);
    return w;
  }
  const FORM = `
    <form>
      <label for="q1">Duties/Responsibilities</label><textarea id="q1" required aria-describedby="q1-err"></textarea><div id="q1-err"></div>
      <label for="q2">If no, why not?</label><input id="q2" type="text">
      <label for="q3">Anything else you want us to know?</label><textarea id="q3"></textarea>
    </form>
    <div id="toast"></div>`;
  {
    const w = textWorld(FORM, { "Duties/Responsibilities": DUTIES, "If no, why not?": WHY_NOT });
    const $ = (id) => w.document.getElementById(id);
    let n1 = 0;
    try { n1 = await w.__text(); } catch (e) { check("fillTextQuestions ran", false, e.message); }
    check("round 1: both AI answers typed in full (no limit known yet)", n1 === 2 && $("q1").value === DUTIES && $("q2").value === WHY_NOT,
      `n=${n1} q1=${$("q1").value.length} q2=${$("q2").value.length}`);
    // The person's own text in q3 (not ours) — must never be rewritten.
    const mine = "x ".repeat(80).trim();
    $("q3").value = mine;
    // What Indeed did on Continue: both fields invalid, one page-level alert.
    for (const id of ["q1", "q2", "q3"]) $(id).setAttribute("aria-invalid", "true");
    $("toast").setAttribute("role", "alert");
    $("toast").textContent = "Answer must be shorter than 100 characters.";
    const askedBefore = w.__asked.length;
    let n2 = 0;
    try { n2 = await w.__text(); } catch (e) { check("fillTextQuestions ran (retry)", false, e.message); }
    check("round 2: both refused answers re-entered at ≤ 99 chars", n2 === 2 && $("q1").value.length <= 99 && $("q2").value.length <= 99 &&
      $("q1").value.length > 0 && $("q2").value.length > 0, `n=${n2} q1=${$("q1").value.length} q2=${$("q2").value.length}`);
    check("…on a boundary, as the start of what the model wrote", DUTIES.startsWith($("q1").value.replace(/\.$/, "")) && WHY_NOT.startsWith($("q2").value.replace(/\.$/, "")),
      `${$("q1").value} | ${$("q2").value}`);
    check("…a value we did not type is never rewritten", $("q3").value === mine);
    check("…and the model was not asked again", w.__asked.length === askedBefore, w.__asked.join(" / "));
    check("…and it is said in the run log", w.__logs.filter((l) => l.startsWith("✂️ Shortened")).length === 2, JSON.stringify(w.__logs));
    const n3 = await w.__text();
    check("round 3: nothing left to shorten → no spin", n3 === 0, `n=${n3}`);
  }
  {
    // maxlength present: the answer is cut BEFORE it is typed.
    const w = textWorld(`<label for="m">Duties/Responsibilities</label><textarea id="m" maxlength="100"></textarea>`,
      { "Duties/Responsibilities": DUTIES });
    await w.__text();
    const v = w.document.getElementById("m").value;
    check("maxlength=100 honoured on the first fill", v.length > 0 && v.length <= 100 && DUTIES.startsWith(v.replace(/\.$/, "")), `${v.length}: ${v}`);
  }

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
