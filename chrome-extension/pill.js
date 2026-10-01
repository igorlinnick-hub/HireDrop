// HireDrop edge pill — a thin handle on the right edge of job pages that opens, on hover,
// into today's count and the one action that matters right now (Stop / Start / Open the
// wall / View). Approved mockup: https://claude.ai/artifact/7VnKDd4so2bJ8Mm1qdhXsF
//
// Ground rules this file keeps, each one a way the pill could hurt the run it reports on:
//
//   1. Never in the campaign's own tab or window. content.js drives that page with CDP
//      clicks at coordinates, and CDP mouse moves are real hovers: a pill that opens under
//      a dispatched move would swallow the next click. The human watches that window
//      anyway; the pill is for the tabs they browse themselves.
//   2. Closed shadow root on a plain <div> hung off <html>, not <body>. content.js reads
//      body text and queries buttons — nothing in here may ever answer those queries.
//   3. No network, no web_accessible_resources. Every asset is inline (SVG mark, CSS
//      gradient for the fan), so a page cannot probe chrome-extension:// URLs to learn
//      HireDrop is installed, and nothing is fetched from the job site's origin.
//   4. Numbers are the extension's own: chrome.storage.local, the same keys GET_STATUS
//      reads (todayCount/todayDate, campaignCaps.dailyTotal). No /stats call per page —
//      the campaign tab alone navigates hundreds of times a day.
//
// Every name at the top level is declared in the isolated world content.js shares, so the
// only top-level names here are hdPill-prefixed; everything else lives inside boot().

const HD_PILL_DEFAULT_DAILY = 30; // background.js DEFAULT_DAILY_TOTAL
const HD_PILL_UNLIMITED = 1_000_000; // subscriptions.py ADMIN_DAILY_LIMIT sentinel

// Pure: storage snapshot -> what the pill shows. Exported for tests/edge-pill.test.js.
function hdPillView(s) {
  const done = s.todayDate === s.today ? s.todayCount || 0 : 0;
  const cap = s.campaignCaps && s.campaignCaps.dailyTotal > 0 ? s.campaignCaps.dailyTotal : HD_PILL_DEFAULT_DAILY;
  const unlimited = cap >= HD_PILL_UNLIMITED;
  const pct = unlimited ? 0 : Math.min(100, Math.round((done / cap) * 100));
  const base = { done, cap: unlimited ? null : cap, pct };

  if (s.captchaWaiting) {
    const terms = s.captchaWaiting.kind === "terms";
    return { ...base, phase: "needs", line: terms ? "Terms page — your turn" : "Captcha — your turn", label: "Open", action: "focus" };
  }
  if (s.campaignRunning) {
    const j = s.currentJob || {};
    const who = j.company || j.title || "";
    return { ...base, phase: "running", line: who ? `Applying · ${who}` : "Working…", label: "Stop", action: "stop" };
  }
  if (!unlimited && done >= cap) {
    return { ...base, phase: "capped", line: "Done for today", label: "View", action: "history" };
  }
  return { ...base, phase: "stopped", line: done > 0 ? "Stopped" : "Not running", label: "Start", action: "dashboard" };
}

// Pure: should this tab show the pill at all?
function hdPillHidden(s) {
  if ((s.pillHiddenHosts || []).includes(s.host)) return true;
  if (!s.campaignRunning) return false;
  if (s.tabId != null && s.tabId === s.campaignTabId) return true;
  if (s.windowId != null && s.windowId === s.campaignWindowId) return true;
  return false;
}

const HD_PILL_KEYS = [
  "campaignRunning", "todayCount", "todayDate", "campaignCaps", "currentJob",
  "captchaWaiting", "campaignTabId", "campaignWindowId", "pillHiddenHosts",
];

function hdPillBoot() {
  if (window.top !== window || window.__hdPillBooted) return;
  window.__hdPillBooted = true;

  const localDay = () => new Date().toLocaleDateString("en-CA"); // same as background.js
  const host = location.hostname;
  let me = { tabId: null, windowId: null };
  let snap = {};
  let el = null; // { root, pill, ... } once mounted
  let closeTimer = null;

  const alive = () => {
    try { return !!chrome.runtime?.id; } catch { return false; }
  };
  const send = (msg) => new Promise((resolve) => {
    if (!alive()) return resolve(null);
    try { chrome.runtime.sendMessage(msg, (r) => { void chrome.runtime.lastError; resolve(r || null); }); }
    catch { resolve(null); }
  });

  const CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .pill {
      --surface: #FFFFFF; --edge: rgba(26,22,14,.13); --handle: #101014; --ring: rgba(255,255,255,.62);
      --ink: #101014; --ink2: #26262F; --muted: #3A3A46; --chip: #F1EFEA; --chip-hover: #E6E3DC;
      --btn: #101014; --btn-ink: #FFFFFF; --track: rgba(16,16,20,.08);
      --ok: #0E9F6E; --off: #8A8A96; --warn: #B07C0A; --warn-ink: #8F6300; --capped: #101014;
      --shadow-open: 0 1px 2px rgba(16,16,20,.08), 0 14px 36px rgba(16,16,20,.16);
      --shadow-shut: 0 2px 8px rgba(16,16,20,.22);
      --fan: linear-gradient(90deg, #2B2466 0%, #4F44C9 22%, #7B6CF6 40%, #E3D9FF 50%, #9B6BF2 62%, #C85FD6 78%, #3A2560 100%);
      --fan-up: linear-gradient(0deg, #4F44C9 0%, #7B6CF6 45%, #B48CF5 75%, #C85FD6 100%);
      position: relative; width: 12px; height: 64px; border-radius: 99px; overflow: hidden;
      box-shadow: var(--shadow-shut);
      font: 400 12px/1.3 Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
      color: var(--ink); -webkit-font-smoothing: antialiased;
      transition: width .3s cubic-bezier(.2,.8,.2,1), height .3s cubic-bezier(.2,.8,.2,1),
                  border-radius .3s cubic-bezier(.2,.8,.2,1), box-shadow .3s ease;
    }
    @media (prefers-color-scheme: dark) {
      .pill {
        --surface: #15131F; --edge: rgba(255,255,255,.12); --handle: #1C1930; --ring: rgba(255,255,255,.42);
        --ink: #FFFFFF; --ink2: #D9D5EC; --muted: #B9B4D0; --chip: rgba(255,255,255,.08); --chip-hover: rgba(255,255,255,.14);
        --btn: #FFFFFF; --btn-ink: #101014; --track: rgba(255,255,255,.12);
        --ok: #34D399; --off: #8E89A6; --warn: #FBBF24; --warn-ink: #FBBF24; --capped: #F4F2FF;
        --shadow-open: 0 14px 40px rgba(0,0,0,.55); --shadow-shut: 0 2px 10px rgba(0,0,0,.5);
      }
    }
    .pill.open { width: 260px; height: 124px; border-radius: 16px; box-shadow: var(--shadow-open); }
    .layer { position: absolute; inset: 0; border-radius: inherit; transition: opacity .2s ease; }
    .layer.shut { background: var(--handle); }
    .layer.card { background: var(--surface); border: 1px solid var(--edge); opacity: 0; }
    .pill.open .layer.shut { opacity: 0; }
    .pill.open .layer.card { opacity: 1; }

    .handle {
      all: unset; position: absolute; top: 0; right: 0; width: 12px; height: 64px; cursor: pointer;
      transition: opacity .16s ease;
    }
    .pill.open .handle { opacity: 0; pointer-events: none; }
    .ring { position: absolute; inset: 3px; border: 1.5px solid var(--ring); border-radius: 99px; overflow: hidden; }
    .fill { position: absolute; left: 0; right: 0; bottom: 0; background: var(--fan-up); }
    .pill[data-phase="running"] .fill { animation: hdBreath 2.4s ease-in-out infinite; }
    .pill[data-phase="stopped"] .fill { opacity: .4; }
    .badge { position: absolute; top: 6px; left: 3px; width: 6px; height: 6px; border-radius: 99px;
             background: #FBBF24; display: none; animation: hdPulse 1.8s ease-out infinite; }
    .pill[data-phase="needs"] .badge { display: block; }

    .card-body {
      position: absolute; top: 0; right: 0; width: 260px; height: 124px; padding: 12px 14px 14px;
      display: flex; flex-direction: column; opacity: 0; pointer-events: none; transition: opacity .2s ease;
    }
    .pill.open .card-body { opacity: 1; pointer-events: auto; transition-delay: .1s; }
    .top { display: flex; align-items: center; gap: 7px; height: 28px; }
    .mark { width: 18px; height: 18px; flex-shrink: 0; }
    .brand { font-size: 12px; font-weight: 600; letter-spacing: -.01em; color: var(--ink); }
    .grow { flex: 1 1 auto; }
    .chip {
      all: unset; width: 28px; height: 28px; border-radius: 99px; background: var(--chip); color: var(--ink);
      display: flex; align-items: center; justify-content: center; cursor: pointer; transition: background .15s;
    }
    .chip:hover { background: var(--chip-hover); }
    .row { margin-top: 10px; display: flex; align-items: center; gap: 10px; }
    .nums { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
    .count { display: flex; align-items: baseline; gap: 5px; line-height: 1; }
    .done { font: 400 26px/1 "Instrument Serif", ui-serif, "New York", Georgia, serif; color: var(--ink); }
    .of { font-size: 13px; font-weight: 500; color: var(--ink2); }
    .line { display: flex; align-items: center; gap: 6px; color: var(--muted); white-space: nowrap; overflow: hidden; }
    .line span:last-child { overflow: hidden; text-overflow: ellipsis; }
    .dot { width: 6px; height: 6px; flex-shrink: 0; border-radius: 99px; background: var(--off); }
    .pill[data-phase="running"] .dot { background: var(--ok); }
    .pill[data-phase="needs"] .dot { background: var(--warn); }
    .pill[data-phase="needs"] .line { color: var(--warn-ink); }
    .pill[data-phase="capped"] .dot { background: var(--capped); }
    .go {
      all: unset; flex-shrink: 0; height: 36px; padding: 0 16px; border-radius: 99px; background: var(--btn);
      color: var(--btn-ink); font-family: inherit; font-size: 13px; font-weight: 600; line-height: 1;
      display: flex; align-items: center;
      gap: 6px; cursor: pointer; transition: opacity .15s;
    }
    .go:hover { opacity: .86; }
    .go:disabled { opacity: .5; cursor: default; }
    .bar { margin-top: auto; height: 4px; border-radius: 99px; background: var(--track); overflow: hidden; }
    .bar i { display: block; height: 4px; border-radius: 99px; background: var(--fan); background-size: 232px 4px;
             transition: width .4s ease; }
    .handle:focus-visible, .chip:focus-visible, .go:focus-visible { outline: 2px solid #6C5CE7; outline-offset: 2px; }
    @keyframes hdBreath { 0%,100% { opacity: .72 } 50% { opacity: 1 } }
    @keyframes hdPulse { 0% { box-shadow: 0 0 0 0 rgba(251,191,36,.6) } 70% { box-shadow: 0 0 0 6px rgba(251,191,36,0) } 100% { box-shadow: 0 0 0 0 rgba(251,191,36,0) } }
    @media (prefers-reduced-motion: reduce) {
      .pill, .layer, .card-body, .bar i { transition: none; }
      .fill, .badge { animation: none !important; }
    }
  `;

  const ICONS = {
    stop: '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
    dashboard: '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7z"/></svg>',
    focus: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M7 17L17 7"/><path d="M8 7h9v9"/></svg>',
  };
  ICONS.history = ICONS.focus;

  function mount() {
    const hostEl = document.createElement("div");
    hostEl.setAttribute("style", [
      "all: initial !important", "position: fixed !important", "top: 50% !important", "right: 8px !important",
      "transform: translateY(-50%) !important", "z-index: 2147483646 !important", "display: block !important",
      "margin: 0 !important", "padding: 0 !important", "border: 0 !important", "width: auto !important",
      "height: auto !important", "pointer-events: auto !important",
    ].join("; "));
    const root = hostEl.attachShadow({ mode: "closed" });
    root.innerHTML = `
      <style>${CSS}</style>
      <div class="pill" part="pill">
        <div class="layer shut"></div>
        <div class="layer card"></div>
        <button class="handle" type="button" aria-label="Open HireDrop">
          <span class="ring"><span class="fill"></span></span>
          <span class="badge"></span>
        </button>
        <div class="card-body" role="group" aria-label="HireDrop">
          <div class="top">
            <svg class="mark" viewBox="0 0 18 18" aria-hidden="true"><rect width="18" height="18" rx="5" fill="#6C5CE7"/><text x="9" y="12.4" text-anchor="middle" font-family="-apple-system, system-ui, sans-serif" font-size="8" font-weight="800" fill="#fff">HD</text></svg>
            <span class="brand">HireDrop</span>
            <span class="grow"></span>
            <button class="chip" type="button" data-do="dashboard" aria-label="Open dashboard" title="Open dashboard">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/></svg>
            </button>
            <button class="chip" type="button" data-do="hide" aria-label="Hide on this site" title="Hide on this site">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="6" y1="12" x2="18" y2="12"/></svg>
            </button>
          </div>
          <div class="row">
            <div class="nums">
              <div class="count"><span class="done">0</span><span class="of"></span></div>
              <div class="line"><span class="dot"></span><span class="txt"></span></div>
            </div>
            <button class="go" type="button" data-do="primary"><span class="ico"></span><span class="lbl"></span></button>
          </div>
          <div class="bar"><i></i></div>
        </div>
      </div>`;
    const q = (s) => root.querySelector(s);
    el = {
      hostEl, pill: q(".pill"), handle: q(".handle"), fill: q(".fill"), done: q(".done"), of: q(".of"),
      txt: q(".txt"), go: q(".go"), ico: q(".ico"), lbl: q(".lbl"), bar: q(".bar i"), view: null,
    };

    const open = () => { clearTimeout(closeTimer); el.pill.classList.add("open"); };
    const shut = () => { clearTimeout(closeTimer); closeTimer = setTimeout(() => el && el.pill.classList.remove("open"), 250); };
    el.pill.addEventListener("mouseenter", open);
    el.pill.addEventListener("mouseleave", shut);
    el.handle.addEventListener("click", open);
    el.pill.addEventListener("focusout", (e) => { if (!el.pill.contains(e.relatedTarget)) shut(); });
    el.pill.addEventListener("keydown", (e) => { if (e.key === "Escape") { el.pill.classList.remove("open"); el.handle.focus(); } });
    root.addEventListener("click", (e) => {
      const b = e.target.closest("[data-do]");
      if (!b) return;
      e.stopPropagation();
      act(b.dataset.do === "primary" ? el.view && el.view.action : b.dataset.do);
    });
    // Keep the page from seeing our clicks/keys as its own (some boards close modals on any
    // document click) — nothing inside the pill is the page's business.
    for (const t of ["click", "mousedown", "mouseup", "keydown", "keyup", "focusin"]) {
      hostEl.addEventListener(t, (e) => e.stopPropagation());
    }
    document.documentElement.appendChild(hostEl);
  }

  function unmount() {
    if (!el) return;
    el.hostEl.remove();
    el = null;
  }

  async function act(what) {
    if (!what) return;
    if (what === "hide") {
      const list = Array.isArray(snap.pillHiddenHosts) ? snap.pillHiddenHosts : [];
      if (!list.includes(host)) await chrome.storage.local.set({ pillHiddenHosts: [...list, host] });
      unmount();
      return;
    }
    if (what === "stop") {
      el.go.disabled = true;
      await send({ type: "STOP_CAMPAIGN", reason: "stopped from the edge pill" });
      if (el) el.go.disabled = false;
      return;
    }
    // Start deliberately goes through the dashboard: the launch there carries the gates a
    // run must pass (required employer answers, US-only, platform pick) — a second Start
    // here would be a second door around them.
    if (what === "focus") return void send({ type: "PILL_FOCUS_HANDOFF" });
    if (what === "history") return void send({ type: "PILL_OPEN", page: "history" });
    return void send({ type: "PILL_OPEN", page: "dashboard" });
  }

  function render() {
    if (!alive()) { unmount(); return; }
    const hidden = hdPillHidden({ ...snap, host, tabId: me.tabId, windowId: me.windowId });
    if (hidden) { unmount(); return; }
    if (!el) mount();
    const v = hdPillView({ ...snap, today: localDay() });
    el.view = v;
    el.pill.dataset.phase = v.phase;
    el.done.textContent = String(v.done);
    el.of.textContent = v.cap ? `/ ${v.cap} today` : "today";
    el.txt.textContent = v.line;
    el.lbl.textContent = v.label;
    el.ico.innerHTML = ICONS[v.action] || "";
    el.bar.style.width = `${v.pct}%`;
    el.fill.style.height = `${Math.max(5, Math.round(v.pct * 0.55))}px`;
    el.handle.setAttribute("aria-label", `Open HireDrop — ${v.done}${v.cap ? ` of ${v.cap}` : ""} today, ${v.line}`);
  }

  async function refresh() {
    if (!alive()) { unmount(); return; }
    try { snap = await chrome.storage.local.get(HD_PILL_KEYS); } catch { unmount(); return; }
    render();
  }

  (async () => {
    const r = await send({ type: "PILL_TAB" });
    if (r) me = { tabId: r.tabId ?? null, windowId: r.windowId ?? null };
    await refresh();
    try {
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== "local") return;
        if (HD_PILL_KEYS.some((k) => k in changes)) refresh();
      });
    } catch {}
    // Local midnight rolls todayCount over without a storage write from anyone.
    setInterval(() => { if (el) render(); }, 60_000);
  })();
}

if (typeof module === "object" && module.exports) {
  module.exports = { hdPillView, hdPillHidden };
} else {
  hdPillBoot();
}
