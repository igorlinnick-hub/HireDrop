// Questions only the person can answer — relocate, live near, on-site, travel, shifts,
// start date — are never guessed, and are asked ONCE (popup) and remembered.
//
//   node <repo>/jobflow/chrome-extension/tests/circumstance-questions.test.js
//
// What is pinned:
//   1. content.js classifies like the backend (modules/personal_facts.topic_of): both run
//      fixtures/circumstance-topics.json.
//   2. The radio filler and the dropdown chooser ask the backend for such a question
//      BEFORE their "Yes"/first-option fallbacks, and leave it blank when there is no
//      answer — the radio default clicked "Yes" on "willing to relocate to Miami?" (10-08).
//   3. The background relays the job's place and tells the person once per question.
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

const radio = slice(CONTENT, "if (!target && circumstanceTopic(groupLabel))", "Generic fallback: exact");
check("radio: circumstance branch sits before the eligibility Yes",
  radio.indexOf("circumstanceAnswer(groupLabel") >= 0 && radio.indexOf("Other eligibility") > radio.indexOf("circumstanceAnswer(groupLabel"));
check("radio: no answer leaves the group blank", /if \(!got\) continue;/.test(radio));

const chooser = slice(CONTENT, "async function chooseOption(", "// Safe fallbacks (no confident answer)");
check("dropdown: circumstance branch returns before the AI budget and the fallbacks",
  chooser.indexOf("circumstanceTopic(label)") >= 0 &&
  chooser.indexOf("circumstanceTopic(label)") < chooser.indexOf("_aiAnswersUsed >= MAX_AI_ANSWERS_PER_FORM") &&
  /return got \? got\.pick : null;/.test(chooser));

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

  // --- 3. the background relay ----------------------------------------------------------

  const relay = slice(BG, 'case "ANSWER_QUESTION": {', 'case "SLEEP"');
  check("relay sends job_location", /job_location: String\(q\.job_location \|\| ""\)/.test(relay));
  check("relay passes ask_person back to the filler", /out\.ask_person = true/.test(relay));
  check("the text filler stops retrying when only the person can answer",
    /if \(res && res\.ask_person\) \{[\s\S]{0,200}break;/.test(CONTENT));
  check("relay tells the person when the server says ask_person",
    /result\.ask_person\) notifyPersonalQuestion\(result\.ask_person\)/.test(relay));
  check("popup answers go to POST /profile/facts",
    /case "ANSWER_PERSONAL_QUESTION"[\s\S]{0,200}apiPost\("\/profile\/facts"/.test(BG));
  check("popup reads GET /personal-questions",
    /case "GET_PERSONAL_QUESTIONS"[\s\S]{0,120}apiGet\("\/personal-questions/.test(BG));

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
