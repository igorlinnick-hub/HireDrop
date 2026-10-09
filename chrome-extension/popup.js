// CONFIG is loaded from config.js via popup.html

const $ = (id) => document.getElementById(id);

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Auth banner helpers
// ---------------------------------------------------------------------------

function showAuthBanner(title, sub) {
  $("auth-banner-title").textContent = title || "Connect Your Account";
  $("auth-banner-sub").textContent =
    sub ||
    "Sign in to HireDrop to start automating your job search. Your session is synced from the dashboard.";
  $("auth-banner").classList.add("visible");
  $("body-main").style.display = "none";
}

function hideAuthBanner() {
  $("auth-banner").classList.remove("visible");
  $("body-main").style.display = "";
}

$("btn-connect").addEventListener("click", () => {
  chrome.tabs.create({ url: CONFIG.DASHBOARD_URL + CONFIG.CONNECT_PATH });
});

// ---------------------------------------------------------------------------
// CAPTCHA alert helpers
// ---------------------------------------------------------------------------

let _captchaTabId = null;
let _captchaUrl = null;

// Two hand-offs share this banner: a captcha ("prove you're human") and a consent wall
// ("accept our terms"). They need different words — pointing someone at a challenge that
// isn't on screen just makes them hunt. `waiting` is chrome.storage.captchaWaiting.
function showCaptchaAlert(waiting) {
  const w = waiting || {};
  _captchaUrl = w.url || null;
  // The storage-backed path carries a pretty platform name; the live runtime message from
  // content.js carries only the URL. Fall back to its host rather than "The site".
  let site = w.site || "";
  if (!site && w.url) { try { site = new URL(w.url).hostname.replace(/^www\./, ""); } catch {} }
  if (!site) site = "The site";
  if (w.kind === "terms") {
    $("captcha-alert-title").textContent = "⏸ Terms need accepting";
    $("captcha-alert-sub").textContent =
      `${site} is asking you to accept its terms. ${w.action || "Accept them in the campaign window"} — the campaign resumes automatically.`;
  } else {
    $("captcha-alert-title").textContent = "⚠️ CAPTCHA Detected";
    $("captcha-alert-sub").textContent =
      `${site} showed a security challenge. Switch to the campaign window and solve it — the campaign will resume automatically.`;
  }
  $("captcha-alert").classList.add("visible");
}

function hideCaptchaAlert() {
  $("captcha-alert").classList.remove("visible");
  _captchaTabId = null;
  _captchaUrl = null;
}

$("btn-go-indeed").addEventListener("click", () => {
  if (_captchaTabId) {
    chrome.tabs.update(_captchaTabId, { active: true });
    return;
  }
  // The button used to hunt for an indeed.com tab by hard-coded pattern — wrong on every
  // other platform (ZR/Greenhouse/Lever all raise these hand-offs too), so it silently did
  // nothing. Match the host the hand-off actually came from.
  let pattern = "*://*.indeed.com/*";
  try {
    if (_captchaUrl) pattern = `*://${new URL(_captchaUrl).hostname}/*`;
  } catch {}
  chrome.tabs.query({ url: pattern }, (tabs) => {
    if (tabs.length) {
      chrome.tabs.update(tabs[0].id, { active: true });
      if (tabs[0].windowId != null) chrome.windows.update(tabs[0].windowId, { focused: true });
    }
  });
});

// ---------------------------------------------------------------------------
// Inline warning helper
// ---------------------------------------------------------------------------

function showWarn(text) {
  const el = $("start-warn");
  el.textContent = text;
  el.classList.add("visible");
}

function hideWarn() {
  $("start-warn").classList.remove("visible");
}

// ---------------------------------------------------------------------------
// Connection check
// ---------------------------------------------------------------------------

let isConnected = false;

async function checkConnection() {
  try {
    const res = await send({ type: "CHECK_CONNECTION" });
    if (res && res.connected) {
      $("conn-dot").className = "conn-dot ok";
      $("conn-text").textContent = "Connected";
      isConnected = true;
      return true;
    }
  } catch {}
  $("conn-dot").className = "conn-dot err";
  $("conn-text").textContent = "Offline";
  isConnected = false;
  $("btn-start").disabled = true;
  return false;
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------

async function loadProfile() {
  const profile = await send({ type: "GET_PROFILE" });
  if (profile && profile.name) {
    const fullName = [profile.name, profile.last_name].filter(Boolean).join(" ");
    $("profile-name").textContent = fullName;
  }
  return profile;
}

// ---------------------------------------------------------------------------
// Activity log
// ---------------------------------------------------------------------------

// A compact stage rail (Scan → Match → Write → Submit) driven by the latest
// activity, instead of a scrolling wall of chatty log lines. The engine still
// writes activity_log; we just read the newest entry and map it to a stage.
const PROC_STAGES = ["Scan", "Match", "Write", "Submit"];

function stageForText(text) {
  const t = (text || "").toLowerCase();
  if (/submit|applied|\bapply|sent|success|complete|done/.test(t)) return 3;
  if (/cover|writ|fill|tailor|\bform|answer|screen|question/.test(t)) return 2;
  if (/match|score|\bfit\b|rank|evaluat|analy/.test(t)) return 1;
  return 0; // scan / search / warmup / found / resuming / opened …
}

// Strip the decorative symbols the engine prepends (⏭ ✓ ⚠ → 🎯 …) so the line
// reads clean; typography carries the meaning.
function cleanMsg(s) {
  try {
    return (s || "").replace(/[\p{Extended_Pictographic}←-⇿✀-➿]/gu, "").replace(/\s{2,}/g, " ").trim();
  } catch {
    return (s || "").trim();
  }
}

function setStage(active) {
  const fill = $("pr-fill");
  if (fill) fill.style.width = active < 0 ? "0%" : `${(active / (PROC_STAGES.length - 1)) * 100}%`;
  // Brand droplet fills as stages advance: empty when idle → full at Submit.
  const drop = $("pd-fill");
  if (drop) {
    const level = active < 0 ? 0 : (active + 1) / PROC_STAGES.length;
    drop.style.transform = `translateY(${Math.round((1 - level) * 100)}%)`;
  }
  for (let i = 0; i < PROC_STAGES.length; i++) {
    const node = $("pn-" + i);
    if (!node) continue;
    node.classList.toggle("done", active >= 0 && i < active);
    node.classList.toggle("active", active === i);
  }
}

async function renderProcess() {
  const section = $("proc-section");
  if (!section) return;
  const { activity_log, campaignRunning } = await chrome.storage.local.get(["activity_log", "campaignRunning"]);
  const latest = (activity_log || [])[0];
  const running = !!campaignRunning;

  if (!latest) {
    section.classList.remove("active", "attn");
    $("proc-state").textContent = running ? "Starting" : "Idle";
    $("proc-action").textContent = running ? "Warming up…" : "Start a campaign to begin";
    setStage(-1);
    return;
  }

  const isErr = latest.cls === "err";
  section.classList.toggle("active", running && !isErr);
  section.classList.toggle("attn", isErr);
  $("proc-state").textContent = isErr ? "Needs you" : running ? "Working" : "Paused";
  $("proc-action").textContent = cleanMsg(latest.text) || "Working…";
  setStage(running && !isErr ? stageForText(latest.text) : -1);
}

// ---------------------------------------------------------------------------
// Elapsed time formatter
// ---------------------------------------------------------------------------

function formatElapsed(ms) {
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

let campaignStartedAt = null;
let elapsedTimer = null;

function startElapsedTimer() {
  stopElapsedTimer();
  elapsedTimer = setInterval(() => {
    if (campaignStartedAt) {
      $("run-time").textContent = formatElapsed(Date.now() - campaignStartedAt);
    }
  }, 1000);
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Status — update entire UI from GET_STATUS
// ---------------------------------------------------------------------------

async function loadStatus() {
  const status = await send({ type: "GET_STATUS" });
  if (!status || status.error) return;

  const running = status.campaignRunning;

  // Stats
  $("stat-today").textContent = status.todayCount || 0;
  $("stat-week").textContent = status.totalApplications || 0;
  $("stat-total").textContent = status.totalJobs || 0;

  if (running) {
    $("campaign-stopped").style.display = "none";
    $("campaign-running").style.display = "";

    if (status.startedAt) {
      campaignStartedAt = new Date(status.startedAt).getTime();
      $("run-time").textContent = formatElapsed(Date.now() - campaignStartedAt);
      startElapsedTimer();
    }

    if (status.currentJob) {
      $("current-job").style.display = "";
      $("cj-title").textContent = status.currentJob.title || "";
      $("cj-company").textContent = status.currentJob.company || "";
    } else {
      $("current-job").style.display = "none";
    }

    // Show the CAPTCHA alert whenever a hand-off is pending (captchaWaiting in
    // storage) — this survives popup reopen, unlike the DETECTION_TRIPPED
    // runtime message which only reaches an already-open popup. Hide it once
    // the campaign is running normally again.
    if (status.captchaDetected) showCaptchaAlert(status.captchaWaiting);
    else hideCaptchaAlert();
  } else {
    $("campaign-stopped").style.display = "";
    $("campaign-running").style.display = "none";
    campaignStartedAt = null;
    stopElapsedTimer();

    // Show the daily budget (total across platforms), not the per-platform rail —
    // todayCount is a cross-platform total, so pairing it with the per-platform cap
    // read as "12 / 20" even when the real daily budget was 50. dailyLimit is the
    // honest denominator; both now come from the backend (app/db/subscriptions.py).
    const limit = status.dailyLimit || status.limitPerPlatform || 20;
    const today = status.todayCount || 0;
    // ADMIN_DAILY_LIMIT (subscriptions.py) is a 10M "unlimited" sentinel — render it
    // as ∞ like the dashboard does, not as a raw number.
    $("limit-text").textContent = limit >= 1_000_000 ? `${today} today · unlimited` : `${today} / ${limit} today`;
    $("btn-start").disabled = !isConnected;
  }

  renderProcess();
}

// ---------------------------------------------------------------------------
// Start campaign — with profile completeness check
// ---------------------------------------------------------------------------

// Real version from the manifest — the header used to hardcode "v1.3" forever.
try { document.getElementById("hd-version").textContent = "v" + chrome.runtime.getManifest().version; } catch (e) {}

$("btn-start").addEventListener("click", async () => {
  hideWarn();
  $("btn-start").disabled = true;
  $("btn-start").textContent = "Checking...";

  const profile = await send({ type: "GET_PROFILE" });

  // Profile completeness gate
  if (!profile || !profile.name) {
    showWarn("Go to the Dashboard → complete your profile first.");
    $("btn-start").textContent = "Start Campaign";
    $("btn-start").disabled = false;
    return;
  }
  if (!profile.keywords || profile.keywords.length === 0) {
    showWarn("No job keywords set. Open Dashboard → Profile and add keywords like \"marketing manager\".");
    $("btn-start").textContent = "Start Campaign";
    $("btn-start").disabled = false;
    return;
  }
  if (!profile.resume_url) {
    addLog("No resume on server — will use Indeed profile resume if available", "");
  }

  $("btn-start").textContent = "Starting...";

  const filters = {
    keywords: profile.keywords,
    platforms: profile.platforms || ["indeed"],
    location: profile.location || "",
    job_type: profile.job_type || "",
  };

  const res = await send({ type: "START_CAMPAIGN", filters });

  if (res && res.started) {
    addLog("Campaign started — Indeed tab opened", "ok");
  } else if (res?.error === "onboarding_incomplete") {
    addLog("Finish your profile setup on hiredrop.io first — the quiz collects the data we fill applications with.", "err");
  } else {
    addLog("Failed to start: " + (res?.message || res?.error || "unknown"), "err");
  }

  $("btn-start").textContent = "Start Campaign";
  await loadStatus();
});

// ---------------------------------------------------------------------------
// Stop campaign
// ---------------------------------------------------------------------------

$("btn-stop").addEventListener("click", async () => {
  $("btn-stop").disabled = true;
  hideCaptchaAlert();
  const res = await send({ type: "STOP_CAMPAIGN", userStop: true });
  if (res && res.stopped) addLog("Campaign stopped", "");
  $("btn-stop").disabled = false;
  await loadStatus();
});

// ---------------------------------------------------------------------------
// Add log entry helper
// ---------------------------------------------------------------------------

async function addLog(text, cls) {
  const { activity_log } = await chrome.storage.local.get("activity_log");
  const logs = activity_log || [];
  logs.unshift({
    time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    text,
    cls: cls || "",
  });
  if (logs.length > 50) logs.length = 50;
  await chrome.storage.local.set({ activity_log: logs });
  renderProcess();
}

// ---------------------------------------------------------------------------
// Hand-backs — the applications waiting on the user's hands
// ---------------------------------------------------------------------------
//
// The filler hands a job back when a form step refuses it (Indeed's demographic
// screen is the common one: we never fabricate a protected-class answer, so a page
// with no "prefer not to say" option cannot be completed by us). The walk moves on
// immediately — the job is not lost, it is WAITING, and until 09-21 the only place
// that said so was the activity log and History. Both scroll away.
//
// Finishing is the user's own tick: we cannot see the employer's side, so on return
// we ASK rather than assume. Claiming a submit we did not observe would be exactly
// the kind of number this product keeps deleting.

// Job we just sent the user off to finish, awaiting their answer on reopen.
let hbAwaiting = null;

async function renderHandbacks() {
  const card = $("hb-card");
  if (!card) return;

  if (hbAwaiting) {
    card.style.display = "";
    $("hb-title").textContent = "did you finish it?";
    $("hb-list").innerHTML =
      '<div class="hb-ask"><p>' + escapeHtml(hbAwaiting.job_title || "That application") +
      '</p><button class="hb-yes" id="hb-yes">Sent it</button>' +
      '<button class="hb-no" id="hb-no">Not yet</button></div>';
    $("hb-yes").addEventListener("click", async () => {
      const id = hbAwaiting.id;
      hbAwaiting = null;
      await send({ type: "RESOLVE_HANDBACK", id });
      renderHandbacks();
    });
    $("hb-no").addEventListener("click", () => { hbAwaiting = null; renderHandbacks(); });
    return;
  }

  const res = await send({ type: "GET_HANDBACKS" });
  const items = (res && res.handbacks) || [];
  if (!items.length) { card.style.display = "none"; return; }

  card.style.display = "";
  $("hb-title").textContent =
    items.length === 1 ? "1 to finish" : items.length + " to finish";
  // Progress instead of prose: "almost done" and "start over" are different decisions,
  // and a percentage answers that in a glance. steps_done is a count we observed; the
  // remaining screen is the one that refused us, hence done/(done+1).
  $("hb-list").innerHTML = items
    .map((h, i) => {
      const d = Math.max(0, Number(h.steps_done) || 0);
      const pct = d > 0 ? Math.round((d / (d + 1)) * 100) : null;
      return '<div class="hb-item"><div class="hb-job"><b>' +
        escapeHtml(h.job_title || h.company || (h.platform ? h.platform[0].toUpperCase() + h.platform.slice(1) + " application" : "Application")) + '</b>' +
        (pct === null ? '<span>' + escapeHtml(h.company || "") + '</span>'
          : '<div class="hb-bar"><i style="width:' + pct + '%"></i></div>' +
            '<span>' + pct + '% done</span>') +
        '</div><button class="hb-go" data-i="' + i + '">Finish</button></div>';
    })
    .join("");

  $("hb-list").querySelectorAll(".hb-go").forEach((btn) => {
    btn.addEventListener("click", () => {
      const h = items[Number(btn.dataset.i)];
      if (!h || !h.url) return;
      // Opens on the exact screen the filler stopped at — everything before it is
      // already filled, so this is a question and a Submit, not a re-entry.
      chrome.tabs.create({ url: h.url });
      hbAwaiting = h;
      renderHandbacks();
    });
  });
}

renderHandbacks();

// ---------------------------------------------------------------------------
// Questions only the person can answer — asked once, remembered for every form
// ---------------------------------------------------------------------------
//
// "Are you willing to relocate to Miami?", "Do you live within 30 miles of Austin?":
// no resume says, so the filler leaves them blank and the form comes back. The answer
// given here is kept (POST /profile/facts) and every later form that asks the same thing
// is filled from it; the jobs this one question was blocking go back to the queue.
// When the person said something on the same topic before ("Moving to San Diego"), it is
// shown, and they choose to keep both answers or replace the earlier one.

let pqDone = null; // the confirmation line after an answer, until the next render

function pqJobsLine(q) {
  const jobs = q.jobs || [];
  if (!jobs.length) return "";
  const first = jobs[0].company || jobs[0].title || "a job";
  return jobs.length === 1
    ? `Waiting on this: ${first}`
    : `Waiting on this: ${first} and ${jobs.length - 1} more`;
}

async function answerPersonalQuestion(q, answer) {
  const replace = $("pq-replace");
  const res = await send({
    type: "ANSWER_PERSONAL_QUESTION",
    data: {
      question: q.question,
      answer,
      replace_ids: replace && replace.checked ? (q.related || []).map((f) => f.id) : [],
    },
  });
  if (!res || !res.ok) {
    const err = $("pq-err");
    if (err) {
      err.textContent = res && res.status === 503
        ? "Can't save answers yet — the server is being updated. Try again later."
        : "Couldn't save that — try again.";
    }
    return;
  }
  pqDone = res.requeued
    ? `Remembered. ${res.requeued === 1 ? "1 job is" : res.requeued + " jobs are"} back in the queue.`
    : "Remembered. We won't ask this again.";
  renderPersonalQuestions();
  renderHandbacks();
}

async function renderPersonalQuestions() {
  const card = $("pq-card");
  if (!card) return;
  const res = await send({ type: "GET_PERSONAL_QUESTIONS" });
  const items = (res && res.questions) || [];
  if (!items.length && !pqDone) { card.style.display = "none"; return; }
  card.style.display = "";
  if (!items.length) {
    $("pq-kicker").textContent = "Thanks";
    $("pq-body").innerHTML = '<div class="pq-done">' + escapeHtml(pqDone) + "</div>";
    pqDone = null;
    return;
  }
  const q = items[0];
  $("pq-kicker").textContent = items.length > 1
    ? `Only you can answer this · 1 of ${items.length}`
    : "Only you can answer this";
  const earlier = (q.related || [])[0];
  const opts = q.options || [];
  const asButtons = opts.length && opts.length <= 6; // a long list (states, years) is a select
  $("pq-body").innerHTML =
    (pqDone ? '<div class="pq-done" style="margin-bottom:8px">' + escapeHtml(pqDone) + "</div>" : "") +
    '<div class="pq-q">' + escapeHtml(q.question) + "</div>" +
    '<div class="pq-meta">' + escapeHtml(pqJobsLine(q)) + " · we'll remember your answer</div>" +
    (earlier
      ? '<div class="pq-earlier">Earlier you said: <b>' + escapeHtml(earlier.question) + "</b> — " +
        escapeHtml(earlier.answer) +
        '<label><input type="checkbox" id="pq-replace"> This replaces that answer</label></div>'
      : "") +
    (asButtons
      ? '<div class="pq-opts">' + opts.map((o, i) =>
          '<button data-i="' + i + '">' + escapeHtml(o) + "</button>").join("") + "</div>"
      : opts.length
        ? '<div class="pq-text"><select id="pq-input"><option value="">Choose…</option>' +
          opts.map((o) => '<option>' + escapeHtml(o) + "</option>").join("") +
          '</select><button id="pq-save">Save</button></div>'
        : '<div class="pq-text"><input id="pq-input" maxlength="500" placeholder="Your answer">' +
          '<button id="pq-save">Save</button></div>') +
    '<div class="pq-err" id="pq-err"></div>';
  pqDone = null;
  if (asButtons) {
    $("pq-body").querySelectorAll(".pq-opts button").forEach((btn) => {
      btn.addEventListener("click", () => answerPersonalQuestion(q, opts[Number(btn.dataset.i)]));
    });
  } else {
    const save = () => {
      const v = ($("pq-input").value || "").trim();
      if (v) answerPersonalQuestion(q, v);
    };
    $("pq-save").addEventListener("click", save);
    $("pq-input").addEventListener("keydown", (e) => { if (e.key === "Enter") save(); });
  }
}

renderPersonalQuestions();

// ---------------------------------------------------------------------------
// Dashboard button
// ---------------------------------------------------------------------------

$("btn-dash").addEventListener("click", () => {
  chrome.tabs.create({ url: CONFIG.DASHBOARD_URL + "/dashboard" });
});

// Edge pill (pill.js) can be hidden per site from its "−" chip; this is the way back.
async function renderPillHidden() {
  const { pillHiddenHosts } = await chrome.storage.local.get("pillHiddenHosts");
  const list = Array.isArray(pillHiddenHosts) ? pillHiddenHosts : [];
  $("pill-hidden").style.display = list.length ? "" : "none";
  $("pill-hidden-hosts").textContent = list.map((h) => h.replace(/^www\./, "")).join(", ");
}

$("btn-pill-show").addEventListener("click", async () => {
  await chrome.storage.local.set({ pillHiddenHosts: [] });
  renderPillHidden();
});

renderPillHidden();

// Edge pill on every site — optional <all_urls> access (pill-everywhere.js). request() must
// be the first thing the click does: it needs the user gesture, and Chrome's prompt usually
// closes this popup, so the background registers the script on permissions.onAdded instead
// of anything here running after the answer.
const PILL_ALL = { origins: ["<all_urls>"] };

async function renderPillEverywhere() {
  let on = false;
  try { on = await chrome.permissions.contains(PILL_ALL); } catch {}
  $("pill-everywhere").style.display = "";
  $("pill-everywhere-text").textContent = on ? "Edge pill on every site" : "Edge pill on job sites only";
  $("btn-pill-everywhere").textContent = on ? "Job sites only" : "Show it on every site";
  $("btn-pill-everywhere").dataset.on = on ? "1" : "";
}

$("btn-pill-everywhere").addEventListener("click", () => {
  const on = $("btn-pill-everywhere").dataset.on === "1";
  const ask = on ? chrome.permissions.remove(PILL_ALL) : chrome.permissions.request(PILL_ALL);
  ask.catch(() => {}).finally(renderPillEverywhere);
});

renderPillEverywhere();

// LinkedIn beta — DEV ONLY (linkedin-beta.js, docs/handoff/linkedin.md). Shown on unpacked
// builds, or anywhere the flag is somehow on (so it can always be turned off). Turning it on
// asks for linkedin.com alone — inside optional_host_permissions, so no manifest change and
// no Web Store warning. request() goes first in the click (it needs the gesture and the
// prompt may close this popup); the background registers content.js on the flag/permission
// events, never from here.
const LI_ORIGINS = { origins: ["https://www.linkedin.com/*"] };

function liDevMsg(text) {
  $("li-dev-msg").textContent = text || "";
}

async function renderLinkedInDev() {
  let dev = false;
  try { dev = (await chrome.management.getSelf()).installType === "development"; } catch {}
  let on = false;
  try { on = (await chrome.storage.local.get("linkedinBeta")).linkedinBeta === true; } catch {}
  if (!dev && !on) return;
  let granted = false;
  try { granted = await chrome.permissions.contains(LI_ORIGINS); } catch {}
  $("li-dev").style.display = "";
  $("li-dev-text").textContent = !on
    ? "LinkedIn beta (dev): off"
    : granted ? "LinkedIn beta (dev): on" : "LinkedIn beta (dev): on, linkedin.com not granted";
  $("btn-li-dev").textContent = !on ? "Turn on" : granted ? "Turn off" : "Grant access";
  $("btn-li-dev").dataset.on = on ? "1" : "";
  $("btn-li-dev").dataset.granted = granted ? "1" : "";
  $("li-dev-capture").style.display = on && granted ? "" : "none";
}

$("btn-li-dev").addEventListener("click", () => {
  const on = $("btn-li-dev").dataset.on === "1";
  const granted = $("btn-li-dev").dataset.granted === "1";
  if (on && granted) {
    chrome.storage.local.set({ linkedinBeta: false }).catch(() => {}).finally(renderLinkedInDev);
    return;
  }
  const ask = chrome.permissions.request(LI_ORIGINS);
  if (!on) chrome.storage.local.set({ linkedinBeta: true }).catch(() => {});
  ask.catch(() => {}).finally(renderLinkedInDev);
});

$("btn-li-capture").addEventListener("click", async () => {
  liDevMsg("Capturing…");
  let tab = null;
  try { [tab] = await chrome.tabs.query({ active: true, currentWindow: true }); } catch {}
  if (!tab || !/^https:\/\/www\.linkedin\.com\//.test(tab.url || "")) {
    liDevMsg("Open the LinkedIn page in this window first.");
    return;
  }
  let res = null;
  try { res = await chrome.tabs.sendMessage(tab.id, { type: "HD_LINKEDIN_CAPTURE" }); } catch {}
  if (!res || !res.ok) {
    liDevMsg(res && res.error === "flag_off"
      ? "The beta flag is off on that page."
      : "No HireDrop script on that tab yet. Reload the LinkedIn tab and try again.");
    return;
  }
  const url = URL.createObjectURL(new Blob([res.html], { type: "text/html" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = res.filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  liDevMsg(`Saved ${res.filename} (${Math.round(res.bytes / 1024)} KB). Read it before committing.`);
});

renderLinkedInDev();

// ---------------------------------------------------------------------------
// Message listener — LOG, AUTH_EXPIRED, DETECTION_TRIPPED
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "LOG") {
    addLog(msg.text, msg.cls || "");
    // If CAPTCHA resolved, hide alert
    if (msg.text && msg.text.includes("CAPTCHA resolved")) hideCaptchaAlert();
    if (msg.cls === "ok") loadStatus();
  }
  if (msg.type === "AUTH_EXPIRED") {
    showAuthBanner(
      "Session Expired",
      "Your HireDrop session has expired. Open the dashboard and log in again — the extension will reconnect automatically."
    );
  }
  if (msg.type === "DETECTION_TRIPPED") {
    const d = msg.data || {};
    _captchaTabId = d.tabId || null;
    const isTerms = d.kind === "terms";
    showCaptchaAlert({ kind: d.kind, action: d.action, url: d.url, site: d.site });
    addLog(
      isTerms
        ? `⏸ Terms need accepting — ${d.action || "accept them in the campaign window"}`
        : `⚠️ CAPTCHA / security check — solve it in the campaign window`,
      isTerms ? "warn" : "err"
    );
  }
});

// ---------------------------------------------------------------------------
// Polling — refresh status every 2 seconds
// ---------------------------------------------------------------------------

let _isAuthenticated = false;

function startPolling() {
  setInterval(async () => {
    if (!_isAuthenticated) {
      const authStatus = await send({ type: "GET_AUTH_STATUS" });
      if (authStatus && authStatus.authenticated) {
        _isAuthenticated = true;
        hideAuthBanner();
        renderProcess();
        const connected = await checkConnection();
        if (connected) await Promise.all([loadProfile(), loadStatus()]);
      }
    } else {
      loadStatus();
    }
  }, 2000);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async () => {
  const authStatus = await send({ type: "GET_AUTH_STATUS" });
  if (!authStatus || !authStatus.authenticated) {
    showAuthBanner();
    startPolling();
    return;
  }

  _isAuthenticated = true;
  hideAuthBanner();
  renderProcess();
  const connected = await checkConnection();
  if (connected) await Promise.all([loadProfile(), loadStatus()]);
  startPolling();
})();
