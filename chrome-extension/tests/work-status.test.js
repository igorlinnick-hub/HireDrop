// Work authorization / sponsorship / consent are answered ONLY as the person would — one
// decision (answerWorkStatus) for radio groups, dropdowns, text boxes and checkboxes.
// Run:
//
//   node <repo>/jobflow/chrome-extension/tests/work-status.test.js
//
// The bug (audit 10-06): "Are you legally authorized to work in Canada?" went out as "Yes"
// whatever the country — the radio path never read `work_authorized_us` at all, the dropdown
// path read it but not the country — and "No, I don't need sponsorship" was clicked on any
// visa radio without reading `needs_sponsorship`. A bare "consent|agree" substring also made
// "Are you subject to any employment AGREEments with your current employer?" a consent → Yes.
//
// QUESTION TEXTS are real: copied verbatim from data/gh_form_schemas.jsonl (320 Greenhouse
// application forms pulled from the boards API; board/job id noted per label). 28 of the
// 320 ask about a country other than the US.
// DOM SHAPES are SYNTHETIC: the smallest fieldset/radio/checkbox markup that carries those
// labels. No captured form in tests/fixtures/ has a non-US work-status question or a
// checkbox group, so these pin our decision logic — not a claim about any board's markup.
// The one captured form (gh-doordash-form.html, DoorDash, US questions) is driven by
// gh-combo-labels.test.js and must keep answering from the profile.

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
function between(a, b) {
  const s = SRC.indexOf(a);
  const e = SRC.indexOf(b, s);
  if (s < 0 || e < 0) { console.error(`markers moved: [${a}] .. [${b}]`); process.exit(2); }
  return SRC.slice(s, e);
}

const WS_BLOCK = between("  // ── Legal work status: ONE reading", "  // Pick a dropdown option deterministically");
const CHOOSERS =
  between("  const PAY_SRC =", "  // Demographic / EEO self-identification") +
  between("  // Demographic / EEO self-identification", "  // ── Legal work status: ONE reading") +
  WS_BLOCK +
  between("  // Pick a dropdown option deterministically", "  // Fill required/empty <select> dropdowns.") +
  between("  // Shared option chooser:", "  // Fill custom (non-native) dropdowns");

// A stand-in for the backend's /tools/answer-question: `saved` maps a question to the
// answer the PERSON gave for this job (the hand-back loop); anything else is refused ("").
function makeCtx(saved = {}, extra = {}) {
  const asked = [];
  const ctx = {
    _aiAnswersUsed: 0, MAX_AI_ANSWERS_PER_FORM: 15, _aiBudgetNotified: false,
    log() {}, logBackend() {},
    sendMsg: async (m) => { asked.push(m.data.question); return { answer: saved[m.data.question] || "" }; },
    asked,
    ...extra,
  };
  vm.createContext(ctx);
  return ctx;
}
const US = { work_authorized_us: true, needs_sponsorship: false };
const NEEDS_VISA = { work_authorized_us: true, needs_sponsorship: true };
const yesNo = ["Yes", "No"].map((text) => ({ text }));
const noYes = ["No", "Yes"].map((text) => ({ text }));

// Real labels, data/gh_form_schemas.jsonl.
const FOREIGN = [
  "Are you legally authorized to work in Canada for Marqeta?",                 // marqeta
  "Do you have the right to work in the UK? ",
  "Are you legally authorized to work in the country where the job is located? (UK or Qatar)",
  "Are you currently legally authorized to work in Mexico or Costa Rica?",
  "Are you authorized to work in Singapore without sponsorship?",
  "Do you need sponsorship to work from Mexico?",
  "Will you require sponsorship to work within the UK now or in the future?",
  "Do you have an EU passport or a valid work visa/permit?",
  "Are you eligible to work in the EU without sponsorship?",
  "Are you legally authorized to work in Barcelona?",
  "Do you now, or will you in the future, require immigration sponsorship to work for Affirm in Canada?",
  "Are you currently based in Singapore and legally authorized to work in Singapore without requiring employer sponsorship now or in the future",
];
const DOMESTIC = [
  ["Are you legally authorized to work in the United States?", "auth"],
  ["Are you legally authorized to work in the country in which this job is located?", "auth"],
  ["Are you legally authorised to work full-time in the country where this job is based?", "auth"],
  ["Will you now or in the future require sponsorship for employment visa status?", "sponsor"],
  // Foreign places named only as visa examples / as "skip if" — still the US question.
  ["Are you authorized to work in the US without sponsorship (e.g. TN for Canada/Mexico)?", "auth_without"],
  ["(Skip this question if you are applying to work in Canada or the UK). Do you now or in the future require sponsorship? ", "sponsor"],
];

(async () => {
  // ---- 1. The reading: which questions, and about which country ------------------------
  const W = makeCtx();
  vm.runInContext(`${WS_BLOCK}\nglobalThis.W = { workStatus, isConsentToProcess, isPersonalKnockout };`, W);
  for (const q of FOREIGN) {
    const ws = W.W.workStatus(q, US);
    check(`another country → never the US flag: “${q.trim().slice(0, 60)}”`, ws && ws.foreign && ws.says === null, JSON.stringify(ws));
  }
  for (const [q, kind] of DOMESTIC) {
    const ws = W.W.workStatus(q, US);
    check(`US / no country → the profile: “${q.trim().slice(0, 60)}”`, ws && !ws.foreign && ws.kind === kind && ws.says !== null, JSON.stringify(ws));
  }
  check("the US question with no flag on file is unknown, not Yes",
    W.W.workStatus("Are you legally authorized to work in the United States?", {}).says === null);
  check("lower-case “us” is the pronoun, not the country (work for us in London)",
    W.W.workStatus("Are you authorized to work for us in London?", US).foreign === true);
  check("“the Americas” is wider than the US",
    W.W.workStatus("Are you eligible to work in the Americas?", US).foreign === true);
  check("not a status question → null", W.W.workStatus("Why do you want to work at Discord?", US) === null);

  // ---- 1b. The truth table: question × profile, every cell TRUE for the person ---------
  // fixtures/work-status-matrix.json — the SAME table tests/test_screener_policy.py runs
  // against the backend's _status_from_profile, so browser and server agree. The skeptic's
  // rows (10-06): "without sponsorship" with (not authorized, needs none) said Yes; a
  // clarifying "(Answer No if you can work without sponsorship)" flipped "Do you require
  // sponsorship?"; "U.K." read as the US; "New Mexico"/"Paris, Texas" read as abroad.
  {
    const M = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "work-status-matrix.json"), "utf8"));
    const ctx = makeCtx();
    vm.runInContext(`${CHOOSERS}\nglobalThis.A = answerWorkStatus; globalThis.choose = chooseOption;`, ctx);
    let cells = 0;
    const bad = [];
    for (const row of M.rows) {
      for (let i = 0; i < M.profiles.length; i++) {
        const [a, n] = M.profiles[i];
        const prof = {};
        if (a !== null) prof.work_authorized_us = a;
        if (n !== null) prof.needs_sponsorship = n;
        const want = row.expect[i];
        const text = await ctx.A(row.q, null, prof, {});
        const sel = await ctx.choose(row.q, yesNo, prof, {});
        const gotText = text && text.pick ? text.pick : "";
        const gotSel = sel ? sel.text : "";
        cells++;
        if (gotText !== want || gotSel !== want) bad.push(`(${a},${n}) want “${want}” text “${gotText}” select “${gotSel}” | ${row.q}`);
      }
    }
    check(`matrix: ${cells} cells (${M.rows.length} questions × ${M.profiles.length} profiles), text and select both true`,
      bad.length === 0, bad.slice(0, 6).join("\n        "));
  }

  // ---- 2. Dropdowns / comboboxes (chooseOption) ----------------------------------------
  {
    const ctx = makeCtx();
    vm.runInContext(`${CHOOSERS}\nglobalThis.choose = chooseOption;`, ctx);
    const canada = "Are you legally authorized to work in Canada for Marqeta?";
    check("select: Canada, US-authorized profile → blank (was “Yes”)", (await ctx.choose(canada, noYes, US, {})) === null);
    check("select: …and the backend was asked for the person's own answer", ctx.asked.includes(canada));
    const saved = makeCtx({ [canada]: "No" });
    vm.runInContext(`${CHOOSERS}\nglobalThis.choose = chooseOption;`, saved);
    check("select: Canada with the person's saved answer → that answer", (await saved.choose(canada, yesNo, US, {}))?.text === "No");
    const uk = "Will you require sponsorship to work within the UK now or in the future?";
    check("select: UK sponsorship → blank (was the US needs_sponsorship “No”)", (await ctx.choose(uk, yesNo, US, {})) === null);
    const usQ = "Are you legally authorized to work in the United States?";
    const before = ctx.asked.length;
    check("select: US → the profile's Yes, no backend call", (await ctx.choose(usQ, noYes, US, {}))?.text === "Yes" && ctx.asked.length === before);
    check("select: US, profile says not authorized → No", (await ctx.choose(usQ, yesNo, { work_authorized_us: false }, {}))?.text === "No");
    check("select: US, profile silent, nobody answered → blank (was “Yes”)", (await ctx.choose(usQ, yesNo, {}, {})) === null);
    const skip = "(Skip this question if you are applying to work in Canada or the UK). Do you now or in the future require sponsorship? ";
    check("select: “skip if Canada/UK” sponsorship → the profile's No", (await ctx.choose(skip, yesNo, US, {}))?.text === "No");
    const visas = ["No", "Yes, EU Blue Card", "Yes, F-1 Visa OPT (USA)"].map((text) => ({ text }));
    check("select: a multi-visa list is never guessed (which visa?)",
      (await ctx.choose("Will you now or in the future require sponsorship for employment visa status?", visas, NEEDS_VISA, {})) === null);
    // Consent vs claim (real labels).
    const nonCompete = "Are you subject to any employment agreements and/or post-employment restrictions with your current employer or a past employer?";
    check("select: “employment agreements” is a claim, not consent → blank (was “Yes”)", (await ctx.choose(nonCompete, yesNo, US, {})) === null);
    const deloitte = "Are you currently employed with or have been employed by Deloitte? Deloitte is our external financial auditor and due diligence may be performed prior to employment to ensure independence requirements are met. If you indicate yes, you agree that certain information may be shared.";
    check("select: “employed by Deloitte … you agree that” → blank (was “Yes”)", (await ctx.choose(deloitte, yesNo, US, {})) === null);
    check("select: “acknowledge and agree to our GDPR policy” is consent → Yes",
      (await ctx.choose("Do you acknowledge and agree to our GDPR policy.", noYes, US, {}))?.text === "Yes");
  }

  // ---- 2b. Other wordings of "authorized", with their REAL options, 4 profiles ---------
  // Skeptic 10-06 (node vm, origin/main vs #378 chooseOption on the 320 schemas): these
  // were not recognised as work status, so they fell to the generic "eligible|legally|able
  // to → Yes" fallback — main said No for a not-authorized profile (its old
  // work_authorized_us === false override), #378 said Yes. Unrecognised-but-status-looking
  // wording must be blank, never a guessed Yes. Real labels: neo4j, cision, instacart,
  // duolingo (gh_form_schemas.jsonl); the "US" ones are synthetic Indeed/ZR-style wording.
  {
    const P4 = {
      US, VISA: NEEDS_VISA,
      UNAUTH: { work_authorized_us: false, needs_sponsorship: true },
      UNAUTH_NS: { work_authorized_us: false, needs_sponsorship: false },
    };
    const yna = ["Yes", "No", "NA"].map((text) => ({ text }));
    const CASES = [
      // [label, options, expected per profile US / VISA / UNAUTH / UNAUTH_NS ("" = blank)]
      ["Are you able to legally work in the region you are applying for?", yesNo, ["Yes", "Yes", "No", "No"]],
      ["Are you legally allowed to work in the country you are applying for without restrictions?\n", yesNo, ["Yes", "No", "No", "No"]],
      ["Are you legally entitled to work in Canada?", yesNo, ["", "", "", ""]],
      ["If so, are you eligible or currently in a period of Optional Practical Training (OPT)?", yna, ["", "", "", ""]],
      ["After the OPT, are you eligible for a 24-month OPT extension or are currently in a 24-month OPT extension based upon a degree from a qualifying U.S. institution in Science, Technology, Engineering, or Mathematics after the Optional Practical Training (OPT)?", yna, ["", "", "", ""]],
      ["Can you legally work in the United States?", yesNo, ["Yes", "Yes", "No", "No"]],
      ["Are you legally allowed to work in the US?", yesNo, ["Yes", "Yes", "No", "No"]],
      ["Are you legally entitled to work in the US?", yesNo, ["Yes", "Yes", "No", "No"]],
      ["Are you legally entitled to work in the United States?", yesNo, ["Yes", "Yes", "No", "No"]],
      ["Are you able to legally work in the US?", yesNo, ["Yes", "Yes", "No", "No"]],
    ];
    const cells = [];
    for (const [q, opts, want] of CASES) {
      Object.keys(P4).forEach((pn, i) => cells.push([pn, q, opts, want[i]]));
    }
    const wrong = [];
    for (const [pn, q, opts, want] of cells) {
      const ctx = makeCtx();
      vm.runInContext(`${CHOOSERS}\nglobalThis.choose = chooseOption;`, ctx);
      const got = (await ctx.choose(q, opts, P4[pn], {}))?.text || "";
      if (got !== want) wrong.push(`${pn} want “${want}” got “${got}” | ${q.trim()}`);
    }
    check(`select: ${CASES.length} other wordings × 4 profiles answer as the person (never a generic Yes)`,
      wrong.length === 0, wrong.slice(0, 6).join("\n        "));
  }

  // "In which state do you hold permanent residency?" (Maven Clinic, 4/320 forms, required)
  // is WHERE the person lives — the profile's state, never a blank "citizenship" refusal.
  {
    const states = ["Alabama", "Alaska", "Arizona", "California", "Colorado", "Florida", "Georgia",
      "Hawaii", "Illinois", "New York", "Texas", "Washington"].map((text) => ({ text }));
    const ctx = makeCtx();
    vm.runInContext(`${CHOOSERS}\nglobalThis.choose = chooseOption;`, ctx);
    const got = (await ctx.choose("In which state do you hold permanent residency?", states,
      { ...US, state: "FL" }, {}))?.text || "";
    check("“which state … permanent residency” picks the profile's state (was blank)", got === "Florida", got);
  }

  // ---- 3. The SMS / opt-in rule (pickOptionDeterministic) ------------------------------
  {
    const ctx = makeCtx();
    vm.runInContext(`${CHOOSERS}\nglobalThis.pick = pickOptionDeterministic;`, ctx);
    check("“opt.?in” does not match “option” (audit claim confirmed, rule unchanged)",
      ctx.pick("Which option best describes your current role?", yesNo, US) === null);
    check("“sms” inside a word (“mechanisms”) is not the SMS opt-in (was “No”)",
      ctx.pick("Do you have experience building fraud-prevention mechanisms?", yesNo, US) === null);
    check("a real SMS consent still gets “No”",
      ctx.pick("Do you consent to receive text messages from Playlist regarding your job application? Message and data rates may apply. Reply STOP to unsubscribe at any time.", yesNo, US)?.text === "No");
    check("an opt-in still gets “No”", ctx.pick("Opt-in to SMS updates", yesNo, US)?.text === "No");
  }

  // ---- 4. Radio groups (fillRadioQuestions) — SYNTHETIC DOM ----------------------------
  const RADIO_CODE =
    between("  // Best-effort human-readable label for any form field.", "  // Find a visible element matching") +
    between("  const PAY_SRC =", "  // Demographic / EEO self-identification") +
    between("  // Demographic / EEO self-identification", "  // Pick a dropdown option deterministically") +
    between("  // Pick a dropdown option deterministically", "  // Fill required/empty <select> dropdowns.") +
    between("  // Fill unanswered radio-button screener questions.", "  // Tick required attestation");
  async function radios(html, profile, saved = {}) {
    const { window } = new JSDOM(`<body>${html}</body>`);
    const sb = makeCtx(saved, {
      window, document: window.document,
      CSS: { escape: (s) => String(s) },
      formScope: () => window.document,
      humanClick: async (el) => {
        const t = el.tagName === "LABEL" ? (window.document.getElementById(el.getAttribute("for") || "") || el) : el;
        t.checked = true;
      },
      humanDelay: () => 0, sleep: async () => {},
      storageGet: async () => ({ profile, currentJobInfo: {} }),
    });
    vm.runInContext(`${RADIO_CODE}\ndone = fillRadioQuestions();`, sb);
    const n = await sb.done;
    const on = Array.from(window.document.querySelectorAll("input:checked")).map((i) => i.id);
    return { n, on, asked: sb.asked };
  }
  const group = (name, q, opts) => `<fieldset><legend>${q}</legend>${opts.map((o, i) =>
    `<input type="radio" name="${name}" id="${name}-${i}"><label for="${name}-${i}">${o}</label>`).join("")}</fieldset>`;
  {
    const r = await radios(group("ca", "Are you legally authorized to work in Canada for Marqeta?", ["Yes", "No"]), US);
    check("radio: Canada → nothing picked (was “Yes”)", r.n === 0 && r.on.length === 0, JSON.stringify(r));
    const s = await radios(group("ca", "Are you legally authorized to work in Canada for Marqeta?", ["Yes", "No"]), US,
      { "Are you legally authorized to work in Canada for Marqeta?": "No" });
    check("radio: Canada with the person's saved answer → that answer", s.on.join() === "ca-1", s.on.join());
    const u = await radios(group("us", "Are you legally authorized to work in the United States?", ["No", "Yes"]), US);
    check("radio: US → the profile's Yes", u.on.join() === "us-1", u.on.join());
    const nu = await radios(group("us", "Are you legally authorized to work in the United States?", ["Yes", "No"]), { work_authorized_us: false });
    check("radio: US, profile not authorized → No (was “Yes”: the radio never read the profile)", nu.on.join() === "us-1", nu.on.join());
    // neo4j's wording, not-authorized profile: the radio has the same generic Yes fallback.
    const neo = await radios(group("neo", "Are you able to legally work in the region you are applying for?", ["Yes", "No"]),
      { work_authorized_us: false, needs_sponsorship: true });
    check("radio: “able to legally work in the region”, not authorized → No (was “Yes”)", neo.on.join() === "neo-1", neo.on.join());
    const blank = await radios(group("us", "Are you legally authorized to work in the United States?", ["Yes", "No"]), {});
    check("radio: US, profile silent → nothing picked (was “Yes”)", blank.on.length === 0, blank.on.join());
    const visa = await radios(group("sp", "Will you now or in the future require sponsorship for employment visa status?",
      ["Yes, I will require sponsorship", "No, I will not require sponsorship"]), NEEDS_VISA);
    check("radio: needs a visa → “Yes, I will require” (was “No …” for everyone)", visa.on.join() === "sp-0", visa.on.join());
    const visaUK = await radios(group("sp", "Will you require sponsorship to work within the UK now or in the future?", ["Yes", "No"]), US);
    check("radio: UK sponsorship → nothing picked (was “No”)", visaUK.on.length === 0, visaUK.on.join());
    const nc = await radios(group("nc", "Are you currently subject to a non-compete agreement or an agreement not to solicit customers with your current or prior employer which may prevent you from performing the job for which you are applying?", ["Yes", "No"]), US);
    check("radio: non-compete question → nothing picked (was “Yes” via “agree”)", nc.on.length === 0, nc.on.join());
    // Real label (Twitch, gh_form_schemas.jsonl): main answered No, the first #378 draft
    // Yes — a false export-control claim. Its "visas / work permits" sit after the "?".
    const twitch = "Since obtaining your most recent citizenship, did you afterwards become a permanent resident in any other country/region? This does not include temporary statuses such as student visas or time-limited work permits.";
    const tw = await radios(group("tw", twitch, ["Yes", "No"]), US);
    check("radio: Twitch permanent-residence question, citizen profile → not Yes (blank)", tw.on.length === 0, tw.on.join());
    const gc = await radios(group("gc", "Are you a U.S. citizen or green card holder?", ["Yes", "No"]), { work_authorized_us: true, needs_sponsorship: true });
    check("radio: “U.S. citizen or green card holder?” → blank, never inferred from work authorization", gc.on.length === 0, gc.on.join());
    const age = await radios(group("age", "Are you 18 or older?", ["Yes", "No"]), {});
    check("radio: unrelated eligibility (18+) keeps its Yes", age.on.join() === "age-0", age.on.join());
  }

  // ---- 5. Checkboxes (fillCheckboxes) — SYNTHETIC DOM ----------------------------------
  const CB_CODE =
    `${SRC.match(/const FIELDISH_SELECTOR =\s*\n?\s*'[^']*';/)[0]}\n` +
    [
      ["  function getFieldLabel(el) {", null], ["  function isShownDialog(d) {", null], ["  function visibleApplyDialogs() {", null], ["  function formScope() {", null],
    ].map(([sig]) => {
      const at = SRC.indexOf(sig); const open = SRC.indexOf("{", at + sig.length - 1); let d = 0;
      for (let i = open; i < SRC.length; i++) { if (SRC[i] === "{") d++; else if (SRC[i] === "}" && --d === 0) return SRC.slice(at, i + 1); }
      return "";
    }).join("\n") +
    between("  // Demographic / EEO self-identification", "  // Pick a dropdown option deterministically") +
    between("  // Tick required attestation", "  // ── The cover letter");
  async function boxes(html, profile, saved = {}) {
    const dom = new JSDOM(`<!doctype html><body><main>${html}</main></body>`, { runScripts: "outside-only" });
    const w = dom.window;
    Object.defineProperty(w.HTMLElement.prototype, "offsetParent", { get() { return this.parentElement; } });
    w.CSS = { escape: (s) => String(s) };
    w.humanClick = async (el) => { el.click(); };
    w.sleep = async () => {}; w.humanDelay = () => 0;
    w.storageGet = async () => ({ profile, currentJobInfo: {} });
    w.sendMsg = async (m) => ({ answer: saved[m.data.question] || "" });
    w.logBackend = () => {}; w._aiAnswersUsed = 0; w.MAX_AI_ANSWERS_PER_FORM = 15;
    w.eval(`${CB_CODE}\nwindow.__fill = fillCheckboxes;`);
    const n = await w.__fill();
    return { n, on: Array.from(w.document.querySelectorAll("input:checked")).map((c) => c.id) };
  }
  const cbGroup = (q, opts) => `<fieldset><legend>${q}</legend>${opts.map((o, i) =>
    `<label><input type="checkbox" id="b${i}" required> ${o}</label>`).join("")}</fieldset>`;
  {
    const r = await boxes(cbGroup("Are you legally authorized to work in the United States?", ["Yes", "No"]), US);
    check("checkbox Yes/No group, US → only Yes (was Yes AND No: “authoriz” + required)", r.on.join() === "b0", r.on.join());
    const f = await boxes(cbGroup("Do you have the legal right to work in the UK?", ["Yes", "No"]), US);
    check("checkbox Yes/No group, UK → nothing", f.on.length === 0, f.on.join());
    const st = await boxes(`<label><input type="checkbox" id="s" required> I am legally authorized to work in the UK</label>`, US);
    check("checkbox statement about the UK → not ticked", st.on.length === 0, st.on.join());
    const su = await boxes(`<label><input type="checkbox" id="s" required> I am legally authorized to work in the United States</label>`, US);
    check("checkbox statement about the US, profile authorized → ticked", su.on.join() === "s", su.on.join());
    const nyc = await boxes(cbGroup("Are you currently based in the NYC metro area?", ["Yes", "No"]), US);
    check("checkbox Yes/No factual question → nothing (was both, by `required`)", nyc.on.length === 0, nyc.on.join());
    const priv = await boxes(cbGroup("Please review and acknowledge Cloudflare's Candidate Privacy Policy (cloudflare.com/candidate-privacy-notice/).", ["Acknowledge/Confirm"]), US);
    check("checkbox privacy acknowledgement → still ticked", priv.on.join() === "b0", priv.on.join());
    const bot = await boxes(`<label><input type="checkbox" id="h" required> I confirm that I am a real human being and not an automated bot</label>`, US);
    check("checkbox “I am a real human, not a bot” → never ticked by us", bot.on.length === 0, bot.on.join());
  }

  // ---- 6. Text boxes: the same helper, before every keyword rule -----------------------
  {
    const ctx = makeCtx();
    vm.runInContext(`${WS_BLOCK}\nglobalThis.A = answerWorkStatus;`, ctx);
    check("text: Canada → blank", (await ctx.A("Are you legally authorized to work in Canada?", null, US, {})).pick === null);
    check("text: US → “Yes”", (await ctx.A("Are you legally authorized to work in the United States?", null, US, {})).pick === "Yes");
    check("text: sponsorship “(within 2 years)” → the profile, not the years rule",
      (await ctx.A("Will you require sponsorship within the next 2 years?", null, US, {})).pick === "No");
    const fn = between("  async function fillTextQuestions() {", "  // Open-ended screener question the keyword rules can't map");
    check("text: work status is decided before the name/date/years/state rules",
      fn.indexOf("workStatus(rawLabel, profile)") > 0 &&
      fn.indexOf("workStatus(rawLabel, profile)") < fn.indexOf('label.includes("date")') &&
      fn.indexOf("workStatus(rawLabel, profile)") < fn.indexOf('label.includes("year")'));
    check("text: the old sponsor-only branch is gone (one decision, not two)", !/\(sponsor\|visa\\b\|h-\?1b\|immigration case\)\/i\.test\(label\)\) \{/.test(fn));
  }

  console.log(failures ? `\n${failures} failed` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
