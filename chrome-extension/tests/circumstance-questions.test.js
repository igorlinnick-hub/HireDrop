// Questions only the person can answer — relocate, live near, on-site, travel, shifts,
// start date — are never guessed, and are asked ONCE (popup) and remembered.
//
//   node <repo>/jobflow/chrome-extension/tests/circumstance-questions.test.js
//
// What is pinned (each by running the real code):
//   1. content.js classifies like the backend (modules/personal_facts.topic_of): both run
//      fixtures/circumstance-topics.json.
//   2. The dropdown chooser, the radio filler and the text filler leave such a question
//      blank when there is no answer on file, instead of their "Yes"/first-option
//      defaults, and fill the person's own answer when there is one.
//   3. The background relay sends the job's place, hands `ask_person` back, tells the
//      person once per question, and saves popup answers to /profile/facts.
//   4. The popup asks the question, shows the earlier answer on the same topic, and saves
//      the answer (with "replace" when ticked).

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const ROOT = path.join(__dirname, "..");
const CONTENT = fs.readFileSync(path.join(ROOT, "content.js"), "utf8");
const BG = fs.readFileSync(path.join(ROOT, "background.js"), "utf8");
const POPUP_JS = fs.readFileSync(path.join(ROOT, "popup.js"), "utf8");
const POPUP_HTML = fs.readFileSync(path.join(ROOT, "popup.html"), "utf8");
const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "circumstance-topics.json"), "utf8"));

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

function slice(src, startMarker, endMarker) {
  const a = src.indexOf(startMarker);
  const b = src.indexOf(endMarker, a);
  if (a < 0 || b < 0) throw new Error(`markers not found: ${startMarker}`);
  return src.slice(a, b);
}

// --- 1. the classifier agrees with the backend's table --------------------------------

const classifier = slice(CONTENT, "const CIRCUMSTANCE_EXPERIENCE_RE", "// The person's own answer to a circumstance question");
const box = {};
vm.createContext(box);
vm.runInContext(classifier + "\nglobalThis.__topic = circumstanceTopic;", box);
for (const [question, topic] of CASES) {
  const got = box.__topic(question);
  check(`topic: ${question.slice(0, 50)}`, got === topic, `got ${got}, want ${topic}`);
}

// --- 2. the fillers ask before they fall back -------------------------------------------

// The same extraction filler-honest.test.js uses for the real chooseOption.
function extract(signature) {
  const at = CONTENT.indexOf(signature);
  if (at < 0) throw new Error(`not found: ${signature}`);
  const open = CONTENT.indexOf("{", at + signature.length - 1);
  let depth = 0;
  for (let i = open; i < CONTENT.length; i++) {
    if (CONTENT[i] === "{") depth++;
    else if (CONTENT[i] === "}" && --depth === 0) return CONTENT.slice(at, i + 1);
  }
  throw new Error(`unbalanced: ${signature}`);
}

function chooserWith(reply) {
  const sent = [];
  const ctx = {
    _aiAnswersUsed: 0, MAX_AI_ANSWERS_PER_FORM: 15, _aiBudgetNotified: false,
    sendMsg: async (m) => { sent.push(m); return reply; },
    logBackend() {}, log() {},
  };
  vm.createContext(ctx);
  const helpers = slice(CONTENT, "  const PAY_SRC =", "  // Demographic / EEO self-identification");
  const workStatusAndCircumstance = slice(CONTENT, "  // ── Legal work status: ONE reading", "  // Pick a dropdown option deterministically");
  vm.runInContext(`${helpers}\n${extract("  function isDemographicQuestion(label, optionTexts) {")}\n` +
    `${workStatusAndCircumstance}\n${extract("  function pickOptionDeterministic(label, options, profile) {")}\n` +
    `${extract("  async function chooseOption(label, options, profile, jobInfo) {")}\n` +
    "globalThis.choose = chooseOption;", ctx);
  return { choose: ctx.choose, sent };
}

// Radio groups through the real fillRadioQuestions, on a jsdom form.
function radioWorld(html, reply) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, { runScripts: "outside-only" });
  const w = dom.window;
  w.formScope = () => w.document;
  w.humanClick = async (el) => {
    const t = el.tagName === "LABEL" ? w.document.getElementById(el.getAttribute("for")) : el;
    if (t) t.checked = true;
  };
  w.humanDelay = () => 0;
  w.sleep = async () => {};
  w.storageGet = async () => ({ profile: {}, currentJobInfo: { title: "Designer", company: "Acme", location: "Miami, FL" } });
  w.sendMsg = async () => reply;
  w.log = () => {};
  w.logBackend = () => {};
  w._aiAnswersUsed = 0;
  w.MAX_AI_ANSWERS_PER_FORM = 15;
  w.eval(
    slice(CONTENT, "  // Best-effort human-readable label for any form field.", "  // Find a visible element matching") +
    slice(CONTENT, "  // Demographic / EEO self-identification", "  // Fill required/empty <select> dropdowns.") +
    slice(CONTENT, "  // Fill unanswered radio-button screener questions.", "  // Tick required attestation") +
    "\nwindow.__fill = fillRadioQuestions;");
  return w;
}

const RELOC_RADIOS = `
  <fieldset><legend>Are you willing to relocate to Miami, FL?</legend>
    <input type="radio" name="reloc" id="r-yes"><label for="r-yes">Yes</label>
    <input type="radio" name="reloc" id="r-no"><label for="r-no">No</label></fieldset>`;

// Text questions through the real fillTextQuestions.
function textWorld(html, replies) {
  const dom = new JSDOM(`<!doctype html><body><form>${html}</form></body>`, { runScripts: "outside-only" });
  const w = dom.window;
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", { get() { return this.parentElement; } });
  w.CSS = { escape: (s) => String(s) };
  w.__sent = [];
  w.__logs = [];
  w.sendMsg = async (m) => { w.__sent.push(m); return replies.shift() || { answer: "" }; };
  w.storageGet = async () => ({ profile: {}, currentJobInfo: { title: "Designer", company: "Acme", location: "Miami, FL" } });
  w.formScope = () => w.document;
  w.localDay = () => "2026-10-09";
  w.reactSelectShownValue = () => "";
  w.isReactSelectField = () => false;
  w.humanDelay = () => 0;
  w.sleep = async () => {};
  w.log = () => {};
  w.logBackend = (t) => w.__logs.push(t);
  w.quickSet = (el, v) => { el.value = v; return true; };
  w.typeValue = async (el, v) => { el.value = v; };
  w._aiAnswersUsed = 0;
  w.MAX_AI_ANSWERS_PER_FORM = 15;
  w.LETTER_LABEL_RE = /cover\s*letter|motivation(al)? letter/i;
  w.eval(
    slice(CONTENT, "  // Best-effort human-readable label for any form field.", "  // Find a visible element matching") +
    slice(CONTENT, "  const PAY_SRC =", "  // Demographic / EEO self-identification") +
    slice(CONTENT, "  // ── Legal work status: ONE reading", "  // Pick a dropdown option deterministically") +
    slice(CONTENT, "  const NAME_I18N_FIRST_RE", "  const LABEL_FALLBACKS") +
    extract("  async function fillTextQuestions() {") +
    "\nwindow.__fill = fillTextQuestions;");
  return w;
}

// circumstanceAnswer itself, with the messaging stubbed.
const answerFn = slice(CONTENT, "async function circumstanceAnswer(", "// Pick a dropdown option deterministically");
async function runAnswer({ reply, options, used = 0 }) {
  const sent = [];
  const logs = [];
  const ctx = {
    MAX_AI_ANSWERS_PER_FORM: 15,
    _aiAnswersUsed: used,
    sendMsg: async (m) => { sent.push(m); return reply; },
    logBackend: (t) => logs.push(t),
  };
  vm.createContext(ctx);
  vm.runInContext(answerFn + "\nglobalThis.__answer = circumstanceAnswer;", ctx);
  const out = await ctx.__answer("Are you willing to relocate to Miami, FL?", options,
    { title: "Designer", company: "Acme", location: "Miami, FL" });
  return { out, sent, logs };
}

(async () => {
  const YN = [{ text: "Yes" }, { text: "No" }];
  let r = await runAnswer({ reply: { answer: "No" }, options: YN });
  check("remembered answer picks the matching option", r.out && r.out.pick === YN[1]);
  check("the job's place travels with the question", r.sent[0].data.job_location === "Miami, FL");

  r = await runAnswer({ reply: { answer: "", ask_person: { topic: "relocation" } }, options: YN });
  check("no answer → null (blank field), and the log says it's left for the person",
    r.out === null && /Left for you to answer once/.test(r.logs[0] || ""));

  r = await runAnswer({ reply: { answer: "Maybe" }, options: YN });
  check("an answer that isn't one of the options is not forced in", r.out === null);

  r = await runAnswer({ reply: { answer: "Yes" }, options: YN, used: 15 });
  check("over the per-form budget → blank, no request", r.out === null && r.sent.length === 0);

  // The real dropdown chooser.
  let c = chooserWith({ answer: "", ask_person: { topic: "relocation" } });
  check("dropdown: relocation with nothing on file stays blank (no first-option Yes)",
    (await c.choose("Are you willing to relocate to Miami, FL?", YN, {}, {})) === null);
  check("dropdown: on-site with nothing on file stays blank (not the 'able to' Yes)",
    (await c.choose("Are you able to work on-site 5 days a week?", YN, {}, {})) === null);
  c = chooserWith({ answer: "No" });
  check("dropdown: the person's remembered answer is picked",
    (await c.choose("Are you willing to relocate to Miami, FL?", YN, {}, { location: "Miami, FL" }))?.text === "No");
  check("…and the job's place went with the question", c.sent[0].data.job_location === "Miami, FL");
  c = chooserWith({ answer: "" });
  check("dropdown: an ordinary question keeps its old fallback (only circumstances changed)",
    (await c.choose("Do you have a valid driver's license?", YN, {}, {}))?.text === "Yes");

  // The real radio filler.
  let w = radioWorld(RELOC_RADIOS, { answer: "", ask_person: { topic: "relocation" } });
  let n = await w.__fill();
  check("radio: relocation with nothing on file is left blank",
    n === 0 && !w.document.getElementById("r-yes").checked && !w.document.getElementById("r-no").checked);
  w = radioWorld(RELOC_RADIOS, { answer: "No" });
  n = await w.__fill();
  check("radio: the remembered answer is clicked", n === 1 && w.document.getElementById("r-no").checked);

  // The real text filler: one request, then it stops — the person answers, not a retry.
  w = textWorld('<label for="q1">Are you willing to relocate to Miami, FL?</label><input type="text" id="q1" required>',
    [{ answer: "", ask_person: true }, { answer: "Yes" }]);
  await w.__fill();
  check("text: a circumstance question is asked once, then left for the person",
    w.__sent.length === 1 && w.document.getElementById("q1").value === "", `sent=${w.__sent.length}`);
  check("text: the job's place went with it", (w.__sent[0] || { data: {} }).data.job_location === "Miami, FL");

  // --- 3. the background relay ----------------------------------------------------------

  // The real handleMessage, with the network and Chrome stubbed.
  const handler = slice(BG, "async function handleMessage(msg, sender) {", "\n}\n") + "\n}\n";
  function bgWorld(post, get) {
    const calls = { post: [], get: [], notified: [] };
    const ctx = {
      chrome: { storage: { local: { get: async () => ({}), set: async () => {} } } },
      apiPost: async (path, body) => { calls.post.push({ path, body }); return post(path, body); },
      apiGet: async (path) => { calls.get.push(path); return get ? get(path) : {}; },
      notifyPersonalQuestion: async (ask) => { calls.notified.push(ask); },
      noLongDashes: (t) => t,
      setTimeout,
    };
    vm.createContext(ctx);
    vm.runInContext(handler + "\nglobalThis.handle = handleMessage;", ctx);
    return { handle: ctx.handle, calls };
  }
  let bg = bgWorld(() => ({ answer: "", ask_person: { topic: "relocation", question: "Relocate to Miami?" } }));
  let res = await bg.handle({ type: "ANSWER_QUESTION", data: {
    question: "Relocate to Miami?", options: ["Yes", "No"], job_title: "Designer", company: "Acme", job_location: "Miami, FL" } });
  check("relay: the job's place reaches /tools/answer-question", bg.calls.post[0].body.job_location === "Miami, FL");
  check("relay: ask_person comes back to the filler", res.answer === "" && res.ask_person === true);
  check("relay: the person is told", bg.calls.notified.length === 1);
  bg = bgWorld(() => ({ answer: "No" }));
  res = await bg.handle({ type: "ANSWER_QUESTION", data: { question: "Relocate to Miami?", options: ["Yes", "No"] } });
  check("relay: an answer is passed through and nobody is notified",
    res.answer === "No" && !res.ask_person && bg.calls.notified.length === 0);

  bg = bgWorld(() => ({ ok: true, requeued: 2 }), () => ({ questions: [{ question: "q" }] }));
  res = await bg.handle({ type: "ANSWER_PERSONAL_QUESTION",
    data: { question: "Relocate to Miami?", answer: "No", replace_ids: ["f_00000001"] } });
  const factPost = bg.calls.post[0];
  check("popup answer is saved to /profile/facts as the popup's",
    factPost.path === "/profile/facts" && factPost.body.source === "popup" && factPost.body.replace_ids[0] === "f_00000001");
  check("…and reports how many jobs it freed", res.ok === true && res.requeued === 2);
  res = await bg.handle({ type: "GET_PERSONAL_QUESTIONS" });
  check("popup reads the waiting questions",
    bg.calls.get[0].startsWith("/personal-questions") && res.questions.length === 1);

  // notifyPersonalQuestion: once per question.
  const notifySrc = slice(BG, 'const HD_ASK_NOTIF_PREFIX = "hd-ask|";', "chrome.notifications.onClicked.addListener");
  const store = {};
  const created = [];
  const bgCtx = {
    chrome: {
      storage: { local: {
        get: async (keys) => Object.fromEntries(keys.map((k) => [k, store[k]])),
        set: async (o) => Object.assign(store, o),
      } },
      notifications: { create: (id, opts) => created.push(opts) },
    },
    Date,
  };
  vm.createContext(bgCtx);
  vm.runInContext(notifySrc + "\nglobalThis.__notify = notifyPersonalQuestion;", bgCtx);
  const ask = { question: "Are you willing to relocate to Miami, FL?", related: [{ answer: "Moving to San Diego in December" }] };
  await bgCtx.__notify(ask);
  await bgCtx.__notify({ ...ask, question: "are you willing to relocate to miami, fl? *" });
  check("one notification for the same question asked twice", created.length === 1, `got ${created.length}`);
  check("the notification shows the earlier answer", /Earlier you said: Moving to San Diego/.test(created[0].message));

  // --- 4. the popup card ----------------------------------------------------------------

  const dom = new JSDOM(POPUP_HTML.replace(/<script[\s\S]*?<\/script>/g, ""), { runScripts: "outside-only" });
  const win = dom.window;
  const sentMsgs = [];
  let questions = [{
    question: "Are you willing to relocate to Miami, FL?",
    options: ["Yes", "No"],
    topic: "relocation",
    related: [{ id: "f_00000001", question: "Relocation plans", answer: "Moving to San Diego in December" }],
    jobs: [{ handback_id: "h1", company: "Acme" }, { handback_id: "h2", company: "Beta" }],
  }];
  win.send = async (m) => {
    sentMsgs.push(m);
    if (m.type === "GET_PERSONAL_QUESTIONS") return { ok: true, questions };
    if (m.type === "ANSWER_PERSONAL_QUESTION") { questions = []; return { ok: true, requeued: 2 }; }
    return {};
  };
  win.$ = (id) => win.document.getElementById(id);
  win.escapeHtml = (t) => { const d = win.document.createElement("div"); d.textContent = t; return d.innerHTML; };
  win.renderHandbacks = () => {};
  const popupPart = slice(POPUP_JS, "let pqDone = null;", "\nrenderPersonalQuestions();\n");
  win.eval(popupPart + "\nwindow.__render = renderPersonalQuestions;");
  await win.__render();
  const card = win.document.getElementById("pq-card");
  check("the card shows when a question waits", card.style.display === "");
  check("it shows the question", /relocate to Miami/.test(card.textContent));
  check("it shows how many jobs wait on it", /Acme and 1 more/.test(card.textContent));
  check("it shows the earlier answer on the same topic", /Moving to San Diego in December/.test(card.textContent));
  win.document.getElementById("pq-replace").checked = true;
  const noBtn = [...card.querySelectorAll(".pq-opts button")].find((b) => b.textContent === "No");
  noBtn.click();
  await new Promise((r) => setTimeout(r, 20));
  const saved = sentMsgs.find((m) => m.type === "ANSWER_PERSONAL_QUESTION");
  check("a tap saves the answer", saved && saved.data.answer === "No");
  check("replace sends the earlier fact's id", saved && saved.data.replace_ids[0] === "f_00000001");
  check("then says it's remembered and what it freed", /Remembered\. 2 jobs are back in the queue\./.test(card.textContent));

  process.exitCode = failures ? 1 : 0;
  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
})();
