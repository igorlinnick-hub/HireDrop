// HireDrop content script — Indeed.com auto-apply automation
// Injected on indeed.com pages via manifest content_scripts
//
// Three phases:
//   PHASE 1 — Job list page (/jobs?): scan for "Easily apply" cards, click first
//   PHASE 2 — Job detail page (/viewjob): extract info, generate cover letter, click Apply
//   PHASE 3 — Application form: fill fields, click through steps, submit

(function () {
  // Don't run in iframes (Indeed job list has indeed.com sub-frames that would
  // double-inject and produce duplicate activity log entries).
  if (window !== window.top) return;
  if (window.__hiredrop_loaded) return;
  window.__hiredrop_loaded = true;

  // Per-platform ban-safety rail. The SINGLE source of truth is the backend
  // (app/db/subscriptions.py MAX_PER_PLATFORM); background.js fetches it at campaign
  // start into chrome.storage.local.campaignCaps and we mirror it here. Default is the
  // SAFE number (20) — never the old 50 — so a fetch failure fails safe (fewer apps),
  // not unsafe (real applications past the rail). All the cap-check sites below read
  // this live value, so aligning the number is now a one-line change in the backend.
  let MAX_APPLICATIONS_PER_PLATFORM = 20;

  // AI-answer budget per form (2026-08-09 dev-test: a max-complex form needs up to 20
  // sequential ANSWER_QUESTION round-trips → 10+ min in the throttled window). Cap the
  // AI answers per job; past the cap, leave remaining custom fields empty so the existing
  // validation-block path hands the job back FAST ("too many custom questions — finish it
  // yourself") instead of grinding. Reset at the start of each apply attempt.
  // 8 was tuned for Indeed/ATS forms; ZipRecruiter routinely ships 10-14 screener
  // questions on ONE step, so the cap fired mid-form and handed back jobs we could have
  // finished (live 08-15, Best Buddies: "Too many custom questions (>8)" → hand-back).
  // 15 covers the observed ZR forms at roughly +$0.01 per application. Igor approved the
  // trade explicitly. The cap still exists so a pathological 40-question form hands back
  // fast instead of grinding for ten minutes.
  const MAX_AI_ANSWERS_PER_FORM = 15;
  let _aiAnswersUsed = 0;
  let _aiBudgetNotified = false;

  // Platforms that run under a TIGHTER rail than the number above (backend MAX_PER_PLATFORM
  // mapping, served as limit_by_platform → campaignCaps.byPlatform). LinkedIn's fallback is
  // its own 5 even before the backend answers: a missing number must fail toward fewer
  // applications on the platform that bans fastest, never toward the default 20.
  let CAP_BY_PLATFORM = {};
  const CAP_FALLBACK_BY_PLATFORM = { linkedin: 5 };
  function platformCap(p) {
    const own = CAP_BY_PLATFORM[p];
    if (typeof own === "number" && own > 0) return own;
    return CAP_FALLBACK_BY_PLATFORM[p] || MAX_APPLICATIONS_PER_PLATFORM;
  }
  function mirrorCaps(caps) {
    const pp = caps && caps.perPlatform;
    if (typeof pp === "number" && pp > 0) MAX_APPLICATIONS_PER_PLATFORM = pp;
    const by = caps && caps.byPlatform;
    if (by && typeof by === "object") CAP_BY_PLATFORM = by;
  }

  (async () => {
    try {
      const s = await storageGet("campaignCaps");
      mirrorCaps(s.campaignCaps);
    } catch {}
  })();
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes.campaignCaps) return;
    mirrorCaps(changes.campaignCaps.newValue);
  });

  // =========================================================================
  // Platform detection
  // =========================================================================

  function detectPlatform() {
    const host = window.location.hostname;
    if (host.includes("ziprecruiter.com")) return "ziprecruiter";
    if (host.includes("greenhouse.io")) return "greenhouse";
    if (host.includes("lever.co")) return "lever";
    if (host.includes("ashbyhq.com")) return "ashby";
    if (host.includes("linkedin.com")) return "linkedin";
    return "indeed";
  }

  // Human-readable platform name for user-facing log lines. Every activity
  // message the dashboard shows should name the ACTUAL platform — a Greenhouse
  // campaign logging "Indeed" reads as broken.
  const PLATFORM_LABELS = { indeed: "Indeed", ziprecruiter: "ZipRecruiter", greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby", linkedin: "LinkedIn" };
  function platformLabel() {
    return PLATFORM_LABELS[detectPlatform()] || "Indeed";
  }

  // =========================================================================
  // Account connection detection
  //
  // Auto-apply only works when the user is logged into the platform in this
  // browser. We can't create accounts for them — but we CAN detect login state
  // from the page DOM (no extra permissions needed) and surface it so the
  // dashboard can show "connect" prompts and block doomed campaigns.
  //
  // Signals verified against live DOM (2026-07-07):
  //   Indeed  — logged out: [data-gnav-element-name="SignIn"] / secure.indeed.com/auth link
  //             logged in:  [data-gnav-element-name="AccountMenu" | "SignOut" | "Resume"]
  //   ZipR    — logged out: a[href*="/authn/login"] ("Log In")
  //             logged in:  a[href*="/authn/logout"] / a[href*="/candidate/"]
  //
  // Returns "connected" | "logged_out" | "unknown".
  //
  // WHERE a negative was read matters (2026-09-09). Indeed runs its SEARCH host and its
  // APPLY host as separate session surfaces: www.indeed.com's gnav renders SignIn for
  // sessions that apply perfectly well, so a "logged out" read there is a guess — and an
  // expensive one, because BOTH pre-flight gates (dashboard QuickActions and the
  // START_CAMPAIGN gate below) refuse to launch on the strength of it. Live 09-06: the
  // header stamped indeed=logged_out while a campaign that hopped to Indeed from
  // ZipRecruiter submitted a real application at 05:11 UTC — Igor had to start every run
  // on ZR to get around it.
  // So: a POSITIVE is trusted anywhere (nothing renders an account menu without a
  // session), a NEGATIVE only on the domains where applying actually happens.
  // Mirrored in background.js (INDEED_APPLY_HOSTS) — content scripts don't see config.js.
  // =========================================================================

  // How many consecutive unreadable job pages it takes to call the PLATFORM broken
  // rather than the postings. 5 is far above real-world noise (dead links and ad pages
  // come one or two at a time) and still fires inside a minute of walking.
  const UNREADABLE_STREAK_LIMIT = 5;

  const INDEED_APPLY_HOSTS = ["smartapply.indeed.com", "secure.indeed.com"];
  function onIndeedApplyHost() {
    const host = window.location.hostname;
    return INDEED_APPLY_HOSTS.some((h) => host === h || host.endsWith("." + h));
  }

  function detectPlatformAuth(platform) {
    const p = platform || detectPlatform();
    if (p === "indeed") {
      // Positive first: nothing renders an account menu without a session. Live 10-08:
      // Connect opens secure.indeed.com/auth, and a finished sign-in lands on
      // secure.indeed.com/settings/account — AccountMenu + SignOut in its header — but the
      // host alone read as "the login wall", so the dashboard kept "Signed out — log back
      // in" after the person had logged in, until they happened to open www.indeed.com.
      if (document.querySelector(
        '[data-gnav-element-name="AccountMenu"], [data-gnav-element-name="SignOut"], [data-gnav-element-name="Resume"]'
      )) return "connected";
      // secure.indeed.com's sign-in pages ARE the wall (you only land there when a session
      // is needed) — its sign-in pages, not the whole host (settings live there too).
      const onSignInPage =
        window.location.hostname === "secure.indeed.com" &&
        /^\/(auth|account\/login)\b/.test(window.location.pathname);
      const negative =
        onSignInPage ||
        !!document.querySelector(
          '[data-gnav-element-name="SignIn"], a[href*="secure.indeed.com/auth"], a[href*="/account/login"]'
        );
      if (negative) return onIndeedApplyHost() ? "logged_out" : "unknown";
      return "unknown";
    }
    if (p === "ziprecruiter") {
      // The "Log In" link is server-rendered in the header whenever logged out.
      if (document.querySelector('a[href*="/authn/login"]')) return "logged_out";
      if (document.querySelector('a[href*="/authn/logout"], a[href*="/candidate/"]')) return "connected";
      // FAIL-CLOSED (2026-07-12): no positive marker either way ⇒ unknown. The
      // old "rendered header ⇒ connected" fallback false-positived logged-out
      // pages whose login link didn't match the selector (fresh users saw
      // ZipRecruiter as Connected without ever logging in).
      return "unknown";
    }
    return "unknown";
  }

  // Persist + report the current platform's login state. Runs on every content
  // script load (cheap) so status stays fresh whenever the user visits the site.
  // The header/nav that carries the login signal can render slightly after
  // document_idle, so poll a few times for a DEFINITIVE (non-unknown) answer
  // before giving up — otherwise an early "unknown" would never get corrected.
  /**
   * Indeed's sign-in page with a return address is often a PASS-THROUGH, not a wall: with
   * a live year-long key the site renews the working session and sends the tab straight on
   * to `continue` (background.js platformEntryUrl enters every Indeed walk this way). Read
   * as a wall the instant it loads, that transit wrote a false "logged_out" and could open
   * the 5-minute login pause for a user who was never signed out. So give it the time a
   * renewal takes: if the page navigates, this context dies here and says nothing — which
   * is the right answer. Still here afterwards ⇒ it really is asking for a human.
   */
  const INDEED_RENEW_GRACE_MS = 10000;
  async function settleIndeedAuthTransit() {
    if (window.location.hostname !== "secure.indeed.com") return;
    if (!/[?&]continue=/.test(window.location.search)) return;
    await sleep(INDEED_RENEW_GRACE_MS);
  }

  // Which build read a login state. background.js logoutIsTrustworthy drops a logged_out
  // written by any other build (10-08: a pre-#388 record outlived its detector and refused
  // Start for a signed-in user). null once this context is orphaned — it can't write then.
  function extVersion() {
    try { return chrome.runtime.getManifest().version; } catch { return null; }
  }

  async function reportPlatformAuth() {
    const platform = detectPlatform();
    if (platform === "indeed") await settleIndeedAuthTransit();
    let status = detectPlatformAuth(platform);
    for (let i = 0; i < 8 && status === "unknown"; i++) {
      await sleep(1000);
      status = detectPlatformAuth(platform);
    }
    if (status === "unknown") return status; // still indeterminate — don't store noise
    try {
      const store = await storageGet("platformConnections");
      const conns = store.platformConnections || {};
      // `host` is the record's provenance: background.js drops a logged_out that wasn't
      // read where applying happens, so a search-page guess can never gate a launch.
      conns[platform] = { status, checkedAt: new Date().toISOString(), host: window.location.hostname, extVersion: extVersion() };
      await storageSet({ platformConnections: conns });
      safeSend({ type: "PLATFORM_AUTH", platform, status, host: window.location.hostname });
    } catch { /* storage/runtime unavailable — ignore */ }
    return status;
  }

  // Login state was only ever re-checked on a full content-script load, so a
  // session that appeared or died WITHOUT a navigation — Indeed's SPA login, or
  // a logout landing in an open tab — kept the stale status until the user
  // happened to browse the platform again ("Connect didn't work, later it said
  // Connected", Igor 09-23). Watch the live page instead: re-detect on a slow
  // interval and on tab focus (coming back from the login tab is exactly the
  // moment the answer changes). Report only a DEFINITIVE answer that differs
  // from the stored one, so the storage write and PLATFORM_AUTH stay rare.
  function watchPlatformAuth() {
    const platform = detectPlatform();
    if (platform !== "indeed" && platform !== "ziprecruiter") return;
    let busy = false;
    const recheck = async () => {
      if (busy) return;
      busy = true;
      try {
        const status = detectPlatformAuth(platform);
        if (status !== "unknown") {
          const store = await storageGet("platformConnections");
          const prev = (store.platformConnections || {})[platform];
          if (!prev || prev.status !== status) await reportPlatformAuth();
        }
      } catch { /* storage/runtime unavailable — ignore */ }
      busy = false;
    };
    setInterval(() => { if (!document.hidden) recheck(); }, 45_000);
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) recheck(); });
    // A sign-in that finishes inside the page (no navigation) redraws the header: re-read
    // within ~0.5 s of it instead of on the next 45 s tick. recheck() is a querySelector
    // and writes only when the answer changed, so a busy page costs next to nothing.
    try {
      let pending = null;
      new MutationObserver(() => {
        if (pending || document.hidden) return;
        pending = setTimeout(() => { pending = null; recheck(); }, 500);
      }).observe(document.documentElement, { childList: true, subtree: true });
    } catch { /* no observer in this context — the interval and focus still cover it */ }
  }

  // The new description block ships its own <style> INSIDE the node, so a plain
  // textContent starts with "@layer htmlContent { /* … */ }" — stylesheet text that
  // would travel to the backend and into the cover-letter prompt as if it were the job
  // ad. Clone, drop style/script, then read.
  function readJobDescription(el) {
    if (!el) return "";
    try {
      const c = el.cloneNode(true);
      c.querySelectorAll("style,script").forEach((n) => n.remove());
      return (c.textContent || "").trim();
    } catch {
      return (el.textContent || "").trim();
    }
  }

  // =========================================================================
  // Utilities
  // =========================================================================

  function sendMsg(msg, timeoutMs = 30000) {
    // Resolve null if the service worker never answers (it can be suspended or
    // restarted mid-message in MV3, in which case the callback never fires). Without
    // this, an awaited sendMsg hangs the whole form-fill loop forever — that's what
    // stalled the apply flow on a screener field whose AI answer never came back.
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      try {
        chrome.runtime.sendMessage(msg, (res) => {
          void chrome.runtime.lastError; // swallow "message port closed"
          finish(res);
        });
      } catch {
        finish(null);
      }
      setTimeout(() => finish(null), timeoutMs);
    });
  }

  function log(text, cls) {
    safeSend({ type: "LOG", text, cls: cls || "" });
  }

  function logBackend(text, level) {
    safeSend({ type: "LOG_BACKEND", text, level: level || "info" });
  }

  // The engine's clock. A bare setTimeout is the wrong clock for this window: the
  // campaign window is opened focused:false and usually sits fully covered, which Chrome
  // treats as hidden — timers get aligned to 1s, and after 5 minutes hidden every CHAINED
  // timer (exactly what an await-sleep loop is) is throttled to one tick per MINUTE. #154
  // trimmed the pause VALUES and the form still took 5-6 min: the milliseconds we ask for
  // stop being the milliseconds we get. The service worker's clock is not subject to any
  // of this, so every sleep races an SW timer against the local one:
  //   - normal window: both fire on time, whichever lands first resolves;
  //   - throttled window: the SW reply arrives on time while the local timer is held;
  //   - dead/restarting SW or orphaned context: the local timer still resolves (late
  //     under throttling — i.e. exactly today's behavior, never worse).
  // The guard that matters: a dying SW fires the callback EARLY with lastError and no
  // response. Resolving on that would cut pauses to ~zero and hammer the page — so only
  // a real {ok} reply may finish the wait ahead of the local timer.
  // ---------------------------------------------------------------------------
  // Storage gateway — the orphan guard
  // ---------------------------------------------------------------------------
  //
  // Reloading the extension (an update, a DEV_RELOAD, a manual OFF/ON) orphans this
  // script in every tab it is already running in. The DOM half keeps going; every
  // `chrome.*` call throws "Extension context invalidated". With ~110 raw storage calls
  // in this file, the walk died wherever it happened to be standing and filled the
  // extension's error console with dozens of identical uncaught throws — noise that made
  // the red "Errors" badge worthless as a signal (09-22: the console was pages of them).
  //
  // So no caller touches chrome.storage directly. An orphan reads as "nothing stored",
  // which is the honest answer — there is no extension behind this tab any more — and it
  // is also the SAFE answer: isCampaignRunning() then returns false and the walk stops on
  // its own terms instead of throwing mid-application.
  function contextGone() {
    try {
      return !chrome.runtime || !chrome.runtime.id;
    } catch {
      return true;
    }
  }

  async function storageGet(keys) {
    if (contextGone()) return {};
    try {
      return (await chrome.storage.local.get(keys)) || {};
    } catch {
      return {}; // invalidated between the check and the call
    }
  }

  async function storageSet(obj) {
    if (contextGone()) return false;
    try {
      await chrome.storage.local.set(obj);
      return true;
    } catch {
      return false;
    }
  }

  function safeSend(msg) {
    if (contextGone()) return;
    try {
      const r = chrome.runtime.sendMessage(msg);
      if (r && r.catch) r.catch(() => {});
    } catch {
      /* invalidated between the check and the call */
    }
  }

  async function storageRemove(keys) {
    if (contextGone()) return false;
    try {
      await chrome.storage.local.remove(keys);
      return true;
    } catch {
      return false;
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(); } };
      setTimeout(finish, ms);
      try {
        chrome.runtime.sendMessage({ type: "SLEEP", ms }, (res) => {
          void chrome.runtime.lastError; // swallow "message port closed"
          if (res && res.ok) finish();
        });
      } catch {
        // Extension context invalidated (orphaned script) — the local timer stands alone.
      }
    });
  }

  function rand(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
  }

  // Phase 5.2 — Human-like delays. Real users don't pause uniformly between
  // 2 and 3 seconds; sometimes they glance and act in 700ms, sometimes they
  // read for 30. Log-normal with sensible clamps captures that: most actions
  // land near the median, but the long tail is preserved.
  // Pass the old (min, max) interval and we use the geometric mean as the
  // median — keeps existing call sites readable while the distribution
  // changes underneath.
  function humanDelay(min, max) {
    const median = Math.sqrt(min * max);
    const sigma = 0.55; // ~70% of values within [median/2, median*2]
    const u1 = Math.max(Math.random(), 1e-9);
    const u2 = Math.random();
    const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
    const ms = median * Math.exp(sigma * z);
    return Math.max(80, Math.min(60000, Math.floor(ms)));
  }

  function shouldMisclick() {
    return Math.random() < 0.05;
  }

  // Phase 5.3 — Mouse emulation. DataDome and similar trackers look at
  // whether mousemove events fire on the path to a click; pure .click()
  // calls are anomalous because no human can teleport the cursor. We
  // dispatch a Bezier sequence of mousemove events before the click and
  // a mousedown/mouseup pair around it. dispatchEvent makes them
  // isTrusted=false, but the absence/presence pattern is what gets
  // looked at, not trust.
  let _lastMouseX = null;
  let _lastMouseY = null;

  function _dispatchMouse(type, x, y, target) {
    if (!target) target = document.elementFromPoint(x, y) || document.body;
    if (!target) return;
    const ev = new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: x,
      clientY: y,
      screenX: x,
      screenY: y,
      button: 0,
    });
    try { target.dispatchEvent(ev); } catch {}
  }

  async function moveCursorTo(targetX, targetY) {
    const sx = _lastMouseX ?? window.innerWidth / 2;
    const sy = _lastMouseY ?? window.innerHeight / 2;
    // Quadratic Bezier with a jittered control point — natural arc, not
    // a straight line. Steps proportional to distance.
    const dist = Math.hypot(targetX - sx, targetY - sy);
    const steps = Math.max(8, Math.min(40, Math.floor(dist / 25)));
    const cx = (sx + targetX) / 2 + (Math.random() - 0.5) * Math.min(200, dist * 0.4);
    const cy = (sy + targetY) / 2 + (Math.random() - 0.5) * Math.min(120, dist * 0.4);
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const x = (1 - t) * (1 - t) * sx + 2 * (1 - t) * t * cx + t * t * targetX;
      const y = (1 - t) * (1 - t) * sy + 2 * (1 - t) * t * cy + t * t * targetY;
      _dispatchMouse("mousemove", x, y);
      await sleep(15 + Math.random() * 35);
    }
    _lastMouseX = targetX;
    _lastMouseY = targetY;
  }

  async function humanClick(target) {
    if (!target || typeof target.getBoundingClientRect !== "function") {
      try { target?.click?.(); } catch {}
      return;
    }
    const r = target.getBoundingClientRect();
    // Aim slightly off-center each time — pixel-perfect aim is robotic.
    const jx = (Math.random() - 0.5) * Math.max(2, r.width * 0.4);
    const jy = (Math.random() - 0.5) * Math.max(2, r.height * 0.4);
    const tx = r.left + r.width / 2 + jx;
    const ty = r.top + r.height / 2 + jy;
    await moveCursorTo(tx, ty);
    _dispatchMouse("mouseover", tx, ty, target);
    await sleep(30 + Math.random() * 90);
    _dispatchMouse("mousedown", tx, ty, target);
    await sleep(40 + Math.random() * 100);
    _dispatchMouse("mouseup", tx, ty, target);
    try { target.click(); } catch {}
  }

  // Click slightly off-target, then back. Mimics the corrective re-click
  // that real users perform after a missaim — a signal naive bots don't emit.
  async function performMisclick(target) {
    if (!target || typeof target.getBoundingClientRect !== "function") return;
    const r = target.getBoundingClientRect();
    const dx = (Math.random() < 0.5 ? -1 : 1) * (30 + Math.random() * 70);
    const dy = (Math.random() < 0.5 ? -1 : 1) * (10 + Math.random() * 30);
    const x = Math.min(window.innerWidth - 5, Math.max(5, r.left + r.width / 2 + dx));
    const y = Math.min(window.innerHeight - 5, Math.max(5, r.top + r.height / 2 + dy));
    const decoy = document.elementFromPoint(x, y);
    if (decoy && decoy !== target) {
      try { decoy.click(); } catch {}
      await sleep(humanDelay(400, 900));
    }
  }

  async function isCampaignRunning() {
    const data = await storageGet("campaignRunning");
    return !!data.campaignRunning;
  }

  // Offline = PAUSE, not failure (same rule as the consent wall). A walk that keeps
  // clicking with no network burns jobs on fetch errors and looks exactly like a
  // broken engine. navigator.onLine can lie about HAVING internet, but false reliably
  // means there is none — good enough to park on. Local log only while parked
  // (logBackend can't reach the backend by definition); one durable line on reconnect.
  async function waitForOnline() {
    if (navigator.onLine) return;
    const started = Date.now();
    log("📡 No internet connection — pausing until it's back", "warn");
    await new Promise((resolve) => {
      const timer = setInterval(() => {
        if (navigator.onLine) { clearInterval(timer); resolve(); }
      }, 3000);
      window.addEventListener("online", () => { clearInterval(timer); resolve(); }, { once: true });
    });
    await sleep(2000); // let the connection settle — fetches right after `online` still fail
    const mins = Math.max(1, Math.round((Date.now() - started) / 60000));
    logBackend(`📡 Back online after ~${mins} min offline — resuming the walk`, "info");
  }

  // Phase 5.4 — Session warmup. The pattern "open page → instantly start
  // automating clicks" never happens for a real user. They land on the
  // search page, glance over a few cards, scroll, sometimes scroll back,
  // *then* engage. Detectors that trigger on engage-time-from-pageload
  // (anything < 3-5s is suspicious) catch this. We run a one-shot
  // warmup the first time content.js sees a running campaign on this
  // page, then mark it done so reloads/page transitions don't re-warmup.
  // True if two URLs point at the same job posting. Indeed jobs are identified by the
  // jk/vjk key (the path may be /viewjob, /rc/clk, /jobs — all the same job); everything
  // else falls back to origin+path (query-stripped). Used so pool warmup doesn't re-
  // navigate when it's already sitting on its target job.
  function urlsSameJob(a, b) {
    try {
      const ua = new URL(a), ub = new URL(b);
      const jka = (ua.searchParams.get("jk") || ua.searchParams.get("vjk") || "").toLowerCase();
      const jkb = (ub.searchParams.get("jk") || ub.searchParams.get("vjk") || "").toLowerCase();
      if (jka || jkb) return jka === jkb;
      return ua.origin + ua.pathname === ub.origin + ub.pathname;
    } catch {
      return a === b;
    }
  }

  // Indeed's location box ("where"). Selectors in priority order — the id is Indeed's own;
  // the rest are fallbacks if it is renamed. Never the 'what' box we just typed into.
  // No captured homepage markup in tests/fixtures yet: selector unverified on a live page.
  function findIndeedWhereInput(whatInput) {
    const sels = [
      "#text-input-where",
      'input[name="l"]',
      'input[aria-label*="location" i]',
      'input[placeholder*="city" i]',
    ];
    for (const sel of sels) {
      const el = document.querySelector(sel);
      if (el && el !== whatInput) return el;
    }
    return null;
  }

  // The search params the homepage form can lose. iafilter is left out on purpose: the
  // scan keeps only Easily-apply cards anyway, and counting it would reopen every search.
  const INDEED_SEARCH_PARAMS = ["q", "l", "radius", "jt"];
  // A check older than this is from a SERP that never loaded (CF wall, handed-off board);
  // acting on it later would yank a deep page of the walk back to page 1 of keyword 1.
  const INDEED_SEARCH_CHECK_TTL_MS = 10 * 60 * 1000;

  // Which of the campaign's search params the SERP at `serpHref` does not carry as asked.
  function indeedSearchDrift(serpHref, targetHref) {
    let serp, target;
    try {
      serp = new URL(serpHref).searchParams;
      target = new URL(targetHref).searchParams;
    } catch {
      return [];
    }
    const norm = (v) => String(v || "").trim().replace(/\s+/g, " ").toLowerCase();
    return INDEED_SEARCH_PARAMS.filter((k) => norm(serp.get(k)) !== norm(target.get(k)));
  }

  // First SERP after the warmup's typed search (see sessionWarmup): if it dropped the
  // city/radius/job type, reopen the campaign's own URL ONCE. cf_clearance is set by now,
  // which is the whole reason the first hop was typed. The check is consumed before any
  // navigation, so a SERP Indeed keeps rewriting cannot loop us; if it cannot be consumed
  // (context gone), we do not navigate at all. True = navigating, the caller must stop.
  async function correctFirstIndeedSearch() {
    const { indeedSearchCheck: chk } = await storageGet("indeedSearchCheck");
    if (!chk) return false;
    if (!(await storageRemove("indeedSearchCheck"))) return false;
    if (!chk.url || !(Date.now() - (chk.at || 0) < INDEED_SEARCH_CHECK_TTL_MS)) return false;
    const serp = new URL(window.location.href).searchParams;
    // The walk builds its pages with an explicit `start` (even page 1 carries start=0, see
    // goBackToIndeedJobList); the typed first search never does. Any `start` = not ours.
    if (serp.has("start")) return false;
    const drift = indeedSearchDrift(window.location.href, chk.url);
    if (!drift.length) return false;
    const got = drift.map((k) => `${k}=${serp.get(k) || "none"}`).join(", ");
    log(`First search ignored your filters (${got}) — reopening it with them...`, "");
    logBackend(`📍 First Indeed search came back with ${got} — reopening it with your city/radius/job type`, "info");
    // A person fixing the filters takes a few seconds; back-to-back page loads are what
    // Cloudflare rate-limits (goBackToIndeedJobList waits 15-30 s for the same reason).
    await sleep(humanDelay(4000, 8000));
    if (!(await isCampaignRunning())) return true;
    window.location.href = chk.url;
    return true;
  }

  async function sessionWarmup() {
    // Warmup exists to (a) look human before engaging (anti-bot) and (b) establish a
    // Cloudflare cf_clearance cookie by first landing on a NON-deep-link page (homepage /
    // search) that passes CF's JS challenge, so the later navigation to the real target
    // isn't a cold "bot jump" that trips "Additional Verification Required".
    //
    // On an ATS page (greenhouse/lever) it must never run — the Indeed branch below would
    // navigate the ATS tab away to the board search URL, killing the apply we came here for.
    {
      const p = detectPlatform();
      if (p === "greenhouse" || p === "lever" || p === "ashby") return;
      // LinkedIn: no Cloudflare and we DIRECT-open the Easy-Apply search (background.js), so
      // there's nothing to warm. The scroll-warmup was taking 32-103s in the throttled
      // background window and delaying phase1 forever (live 2026-08-03). Skip it.
      if (p === "linkedin") return;
    }
    // POOL (by-link) mode: background opens the automation window on the platform HOMEPAGE
    // (see background.js homeUrl), NOT on the deep /viewjob link — a cold deep-link nav is a
    // bot jump that Cloudflare challenges. We warm here on the homepage (scroll → CF auto-
    // solves → cf_clearance set), then navigate to the specific target /viewjob, which now
    // carries cf_clearance and passes. Every job is a canonical link, so we must NOT type a
    // search query (the old auto tail did that and looped the pool on the head job forever).
    const poolRun = (await storageGet("atsPlatform")).atsPlatform === "pool";
    const flag = await storageGet("campaignWarmedUp");
    if (flag.campaignWarmedUp) return;
    // A first-search check left by an earlier run/board whose SERP never loaded must not
    // fire on this one; the Indeed branch below re-arms it for this board when it applies.
    await storageRemove("indeedSearchCheck");

    log("Session warmup — looking around for a few seconds...", "");
    const startedAt = Date.now();
    const passes = 2 + Math.floor(Math.random() * 3); // 2-4 scroll passes
    for (let i = 0; i < passes; i++) {
      const dir = Math.random() < 0.5 ? 1 : -1;
      const distance = (200 + Math.random() * 600) * dir;
      try {
        window.scrollBy({ top: distance, behavior: "smooth" });
      } catch {
        window.scrollBy(0, distance);
      }
      // Move the virtual cursor too — gives the next humanClick a sane
      // starting position, also generates extra mousemove events.
      const tx = 100 + Math.random() * (window.innerWidth - 200);
      const ty = 100 + Math.random() * (window.innerHeight - 200);
      await moveCursorTo(tx, ty);
      await sleep(humanDelay(2000, 5000));
    }
    // The passes take 4-20 s. A Stop inside them used to be followed by "Warmup complete —
    // navigating to job search" and the navigation (live 10-08: stopped 02:53:54, navigated
    // 02:53:58). Every await in the walk is a place Stop can land; re-check before acting.
    if (!(await isCampaignRunning())) return;
    const elapsed = Date.now() - startedAt;
    await storageSet({ campaignWarmedUp: true });

    // Navigate to the target search URL if we're not already on it.
    const { campaignTargetUrl } = await storageGet("campaignTargetUrl");

    // POOL (by-link) mode: campaignTargetUrl is a SPECIFIC job (Indeed /viewjob?jk= or a
    // ZR job page), not a search URL. We warmed on the homepage above; now that cf_clearance
    // is set, navigate straight to that job. Do NOT fall through to the typed-search branches
    // (they'd rewrite the URL to a search and the pool would never reach its picked job).
    if (poolRun) {
      if (campaignTargetUrl && !urlsSameJob(window.location.href, campaignTargetUrl)) {
        log(`Warmup done (${Math.round(elapsed / 1000)}s) — opening your picked job`, "ok");
        logBackend(`Warmup complete — opening approved pick`, "ok");
        window.location.href = campaignTargetUrl;
        return;
      }
      log(`Warmup complete (${Math.round(elapsed / 1000)}s) — on picked job`, "ok");
      logBackend(`Session warmup complete (${Math.round(elapsed / 1000)}s) — applying your pick`, "ok");
      return;
    }

    if (campaignTargetUrl) {
      const platform = detectPlatform();

      if (platform === "ziprecruiter") {
        // For ZipRecruiter: navigate directly to the search URL (no form-submit trick needed)
        const targetSearch = new URL(campaignTargetUrl).searchParams.get("search") || "";
        const currentSearch = new URL(window.location.href).searchParams.get("search") || "";
        const notOnSearchPage = !window.location.href.includes("/jobs-search") &&
                                !window.location.href.includes("/candidate/search");
        if ((targetSearch && currentSearch !== targetSearch) || notOnSearchPage) {
          log(`Warmup done (${Math.round(elapsed / 1000)}s) — navigating to ZipRecruiter search`, "ok");
          logBackend(`Warmup complete — navigating to ZipRecruiter search`, "ok");
          window.location.href = campaignTargetUrl;
          return;
        }
        log(`Warmup complete (${Math.round(elapsed / 1000)}s)`, "ok");
        logBackend(`Session warmup complete (${Math.round(elapsed / 1000)}s) — starting job scan`, "ok");
        return;
      }

      // Indeed: prefer typing into search form (avoids Cloudflare Turnstile on direct nav)
      const targetQ = new URL(campaignTargetUrl).searchParams.get("q") || "";
      const currentQ = new URL(window.location.href).searchParams.get("q") || "";
      // The typed search used to carry ONLY `q`: 'where' stayed whatever Indeed remembered
      // and radius/jt were never applied. 10-08 05:16Z live run (city San Diego, radius 25,
      // part-time): the first SERP was Waikiki/O'ahu/Pearl Harbor and the first application
      // went to a Hawaii employer; same on 10-06 19:58Z and 22:42Z (one Honolulu apply).
      // Every later page is a URL built from the filters and was right. So the city is now
      // typed too, and — because the homepage form cannot carry radius/jt — the SERP the
      // walk lands on is checked ONCE against this URL (correctFirstIndeedSearch). Armed on
      // every Indeed path, including "already on a SERP", which is the same bug wearing a
      // different URL. No location → nothing armed, today's behaviour.
      const targetL = new URL(campaignTargetUrl).searchParams.get("l") || "";
      if (targetL) {
        await storageSet({ indeedSearchCheck: { url: campaignTargetUrl, at: Date.now() } });
      }
      // Navigate if keywords don't match OR if we're not on a jobs page at all
      // (e.g. indeed.com homepage when filters have no keywords).
      const notOnJobsPage = !window.location.href.includes("/jobs");
      if ((targetQ && currentQ !== targetQ) || notOnJobsPage) {
        // Use Indeed's search form instead of direct URL navigation.
        // window.location.href = searchUrl triggers Cloudflare Turnstile because it
        // looks like a bot jump; a typed form submission does not.
        const searchInput = document.querySelector(
          '#text-input-what, input[name="q"], input[aria-label*="job title" i], input[placeholder*="job" i]'
        );
        if (searchInput) {
          log(`Warmup done (${Math.round(elapsed / 1000)}s) — typing search query...`, "ok");
          logBackend(`Warmup complete — searching "${targetQ}" via form`, "ok");
          await humanClick(searchInput);
          await sleep(humanDelay(300, 600));
          await typeValue(searchInput, targetQ);
          await sleep(humanDelay(500, 900));
          if (targetL) {
            // typeValue clears the box first, so Indeed's remembered city is replaced, not
            // appended to. A missing box is not fatal: the SERP check reopens the search.
            const whereInput = findIndeedWhereInput(searchInput);
            if (whereInput) {
              await humanClick(whereInput);
              await sleep(humanDelay(300, 600));
              await typeValue(whereInput, targetL);
              await sleep(humanDelay(500, 900));
            } else {
              logBackend(`Indeed search form has no location box — the results will be checked against "${targetL}"`, "warn");
            }
          }
          if (!(await isCampaignRunning())) return;
          const submitBtn = document.querySelector(
            'button[type="submit"], .yosemite_serp_tbl button, [data-testid*="search-button" i]'
          );
          if (submitBtn) {
            await humanClick(submitBtn);
          } else {
            searchInput.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", keyCode: 13, bubbles: true }));
            searchInput.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", keyCode: 13, bubbles: true }));
          }
          return;
        }
        // Fallback if search form not found on current page
        log(`Warmup done (${Math.round(elapsed / 1000)}s) — navigating to job search`, "ok");
        logBackend(`Warmup complete — navigating to job search`, "ok");
        window.location.href = campaignTargetUrl;
        return;
      }
    }
    log(`Warmup complete (${Math.round(elapsed / 1000)}s)`, "ok");
    logBackend(`Session warmup complete (${Math.round(elapsed / 1000)}s) — starting job scan`, "ok");
  }

  async function getPlatformCount(platform) {
    const data = await storageGet(["platformCounts", "todayDate"]);
    const today = localDay();
    if (data.todayDate !== today) return 0;
    const counts = data.platformCounts || {};
    return counts[platform] || 0;
  }

  // Wait for an element matching selector to appear (up to timeoutMs)
  function waitForElement(selector, timeoutMs = 10000) {
    return new Promise((resolve) => {
      const el = document.querySelector(selector);
      if (el) return resolve(el);

      const observer = new MutationObserver(() => {
        const el = document.querySelector(selector);
        if (el) {
          observer.disconnect();
          resolve(el);
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });

      setTimeout(() => {
        observer.disconnect();
        resolve(null);
      }, timeoutMs);
    });
  }

  // Wait for any of multiple selectors
  function waitForAny(selectors, timeoutMs = 10000) {
    return new Promise((resolve) => {
      const check = () => {
        for (const sel of selectors) {
          const el = document.querySelector(sel);
          if (el && el.offsetParent !== null) return el;
        }
        return null;
      };

      const found = check();
      if (found) return resolve(found);

      const observer = new MutationObserver(() => {
        const found = check();
        if (found) {
          observer.disconnect();
          resolve(found);
        }
      });
      observer.observe(document.body, { childList: true, subtree: true });

      setTimeout(() => {
        observer.disconnect();
        resolve(null);
      }, timeoutMs);
    });
  }

  // Wait for a real signal that the application was actually submitted
  // (Phase 3.2 — Verify submission). Without this, every Submit click was
  // counted as 'applied' even if Indeed showed a captcha, error toast,
  // Specific path segments only — broad single words like "submitted"/"success"
  // can appear in intermediate step URLs and cause a false "verified". Module-scope on
  // purpose: waitForSubmissionConfirmation polls it AND the unknown-phase re-init path
  // consults it — a full navigation to the ATS thank-you page kills this script's
  // context mid-wait, so the fresh context waking up ON the confirmation page is the
  // normal way a Greenhouse submit ends, not an edge case.
  // How long the whole run holds for ONE human wall (captcha or a terms gate).
  //
  // Was 2h, set when waiting was the only alternative to killing the run. It isn't any
  // more: these walls are account-wide WITHIN a platform — Indeed's captcha says nothing
  // about Greenhouse — so the run hands the board on and goes earning applications
  // elsewhere. Once there is somewhere to go, the honest wait is minutes: the campaign
  // window sits on the user's screen, so five minutes of silence means they are not at
  // the machine, and the next 115 minutes change nothing. Nothing is lost — the wall
  // stays solvable and the board comes back on the next run (Igor, 09-21).
  const HUMAN_WALL_WAIT_MS = 5 * 60 * 1000;
  const CONSENT_WAIT_MS = HUMAN_WALL_WAIT_MS;

  const POSTAPPLY_URL_HINTS = [
    "/applied", "postapply", "post_apply", "post-apply", "thank-you", "thankyou",
    "/success", "/confirmation", "application-submitted", "applysuccess",
  ];

  function isPostApplyPath(pathname) {
    const p = (pathname || "").toLowerCase();
    return POSTAPPLY_URL_HINTS.some((h) => p.includes(h));
  }

  // Strict form for the submit belt: a hint counts only as a WHOLE path segment (or one
  // with a -/_ suffix: thank-you-for-applying). A substring match read company slugs as
  // confirmations — job-boards.greenhouse.io/appliedintuition/jobs/4001 matched "/applied",
  // and a stale pending entry turned a reload of that FORM into an applications row.
  // Returns the index of the first post-apply segment, or -1.
  function postApplySegmentIndex(segs) {
    const cores = POSTAPPLY_URL_HINTS.map((h) => h.replace(/^\//, ""));
    return segs.findIndex((s) => cores.some((c) => s === c || s.startsWith(c + "-") || s.startsWith(c + "_")));
  }

  // ---- Submit belt: record a full-page ATS submit whichever page wakes up after it ----
  //
  // A Greenhouse Submit is a full page load to /jobs/<id>/confirmation. It kills the
  // phase_ats context before APPLICATION_SAVED goes out, so the record used to depend on
  // the NEW page's script reaching recordWokeOnPostApply — which only the two queue walks
  // call, and only in the campaign tab with the campaign still running. When anything else
  // woke up (Snorkel 09-28: "Staying idle … /con — not the campaign tab"), a sent
  // application left no applications row: backend dedup went blind and the same posting
  // was applied to again (masterclass/8174068 twice, tia/8005735003 attempted 3x).
  //
  // So phase_ats writes `pendingAtsSubmit` right before the click, and init asks this
  // BEFORE any gate: a confirmation page of the SAME posting within 10 minutes of our own
  // click is that submit landing. It records — it never advances a queue or starts work.
  // One record per submit: whoever records (this belt, the walk's recordWokeOnPostApply,
  // phase_ats itself when the context survives) clears the pending key and leaves
  // `lastRecordedSubmit`, which the others check before writing a second row.
  const PENDING_SUBMIT_MAX_AGE_MS = 10 * 60 * 1000;

  // The posting a URL belongs to: host + path without query, the post-apply segment
  // (/confirmation, /thank-you, …) and a trailing /apply|/application. The form URL
  // phase_ats records (location.href minus the query — the same normalisation as
  // appliedUrls) and its confirmation URL map to the same identity.
  function postingIdentity(url) {
    let u;
    try { u = new URL(url, location.href); } catch { return ""; }
    let segs = u.pathname.toLowerCase().split("/").filter(Boolean);
    const i = postApplySegmentIndex(segs);
    if (i > -1) segs = segs.slice(0, i);
    if (segs.length && (segs[segs.length - 1] === "apply" || segs[segs.length - 1] === "application")) segs.pop();
    return `${u.hostname.toLowerCase()}${segs.length ? "/" + segs.join("/") : ""}`;
  }

  async function markSubmitRecorded(url) {
    await storageSet({ lastRecordedSubmit: { identity: postingIdentity(url), ts: Date.now() } });
    await storageRemove("pendingAtsSubmit");
  }

  async function submitAlreadyRecorded(url) {
    const r = (await storageGet("lastRecordedSubmit")).lastRecordedSubmit;
    return !!(r && r.identity && r.identity === postingIdentity(url)
      && Date.now() - (r.ts || 0) < PENDING_SUBMIT_MAX_AGE_MS);
  }

  async function _recordPendingSubmitOnce() {
    if (postApplySegmentIndex(location.pathname.toLowerCase().split("/").filter(Boolean)) < 0) return false;
    const pend = (await storageGet("pendingAtsSubmit")).pendingAtsSubmit;
    if (!pend || !pend.url) return false;
    if (!(Date.now() - (pend.ts || 0) < PENDING_SUBMIT_MAX_AGE_MS)) {
      await storageRemove("pendingAtsSubmit"); // stale: whatever it was, it is not this page
      return false;
    }
    if (postingIdentity(location.href) !== postingIdentity(pend.url)) return false;
    // Claim first, then send: a second wake on this page must find nothing to record.
    await markSubmitRecorded(pend.url);
    logBackend(`⚠️ Applied (unconfirmed — recorded on the confirmation page): ${pend.title} @ ${pend.company || "?"}`, "warn");
    // advance:false — this page may not be the campaign tab, and when it is, the walk
    // branch (recordWokeOnPostApply) advances once. Two advances per submit popped the
    // NEXT pick and PATCHed it "skipped" in pool mode.
    await sendMsg({
      type: "APPLICATION_SAVED",
      advance: false,
      data: {
        job_title: pend.title, company: pend.company || "",
        platform: pend.platform || detectPlatform() || "",
        job_url: pend.url,
        cover_letter: pend.letter || "",
        // Same honesty rule as recordWokeOnPostApply: this context never saw the form.
        status: "applied_unconfirmed", verified: false,
        verify_signal: "pending-submit-postapply-url",
      },
    });
    return true;
  }

  // Memoised per URL: init and a walk branch may both ask on the same page, and two
  // concurrent reads of the pending key would otherwise both record.
  let _pendingBelt = null;
  function recordPendingSubmitOnConfirmation() {
    if (!_pendingBelt || _pendingBelt.href !== location.href) {
      _pendingBelt = { href: location.href, promise: _recordPendingSubmitOnce().catch(() => false) };
    }
    return _pendingBelt.promise;
  }

  /**
   * Woke on a post-apply page? Then this is a SENT application, not a broken job page.
   *
   * phase_ats filled and submitted; the ATS did a FULL navigation to its thank-you URL,
   * which killed that script context before it could record anything — and this re-init
   * woke up on the result. Both queue walks (the tap pool AND the auto ATS walk) share
   * the same atsQueue and the same death, so both must ask this before they skip.
   * History: the pool branch got this check in #217 (live 2026-09-21, Amwell); the auto
   * ATS walk did not, and on 2026-09-27 a Greenhouse submit to Tia landed on
   * /tia/jobs/8005735003/confirmation and was logged "Skipping (posting closed/errored)" —
   * no applications row, dedup blind to the company, a re-apply possible on the next run.
   *
   * URL check only: on a cold re-init the URL is the one thing we know. Returns true when
   * it recorded + advanced (caller must stop), false when this is not a post-apply page.
   */
  async function recordWokeOnPostApply() {
    const _path = location.pathname.toLowerCase();
    if (!POSTAPPLY_URL_HINTS.some((h) => _path.includes(h))) return false;
    // The submit belt (init) or an earlier wake already wrote this submit's row: advance
    // the walk, never write a second one — the applications insert has no dedup.
    if ((await recordPendingSubmitOnConfirmation()) || (await submitAlreadyRecorded(location.href))) {
      logBackend(`Post-apply page reached (${location.hostname}${_path}) — already recorded, next job`, "info");
      // recorded:true — the head was SENT; ATS_JOB_DONE must not PATCH it "skipped".
      await sendMsg({ type: "ATS_JOB_DONE", recorded: true });
      return true;
    }
    const _q = (await storageGet("atsQueue")).atsQueue || [];
    const _cur = _q[0] || {};
    // Unconfirmed, not "applied": the URL says the submit landed, but this
    // context never saw the form succeed — same honesty rule as phase_ats.
    if (_cur.title) {
      // The letter went to the employer — this context just never saw it.
      // Every generation path stores it (with currentJobInfo alongside), and
      // the normal report branch reads the same key; writing "" here made a
      // sent-with-letter application indistinguishable in the database from
      // one sent without (live: Amwell 09-21, Glossier 07-19, both GH).
      // Guarded by currentJobInfo: the key holds the LAST generation, so it
      // is only ours if it was generated for the job the queue head names.
      const _st = await storageGet(["generatedCoverLetter", "currentJobInfo"]);
      const _for = _st.currentJobInfo || {};
      const _sameJob = _for.title && _cur.title
        && _for.title.trim().toLowerCase() === _cur.title.trim().toLowerCase();
      const _letter = _sameJob ? (_st.generatedCoverLetter || "") : "";
      logBackend(`⚠️ Applied (unconfirmed — woke on the confirmation page): ${_cur.title} @ ${_cur.company || "?"}${_letter ? "" : " (letter not recovered)"}`, "warn");
      await sendMsg({
        type: "APPLICATION_SAVED",
        data: {
          job_title: _cur.title, company: _cur.company || "",
          platform: detectPlatform() || _cur.platform || "",
          job_url: _cur.applyUrl || location.href,
          cover_letter: _letter,
          status: "applied_unconfirmed", verified: false,
          verify_signal: "reinit-postapply-url",
        },
      });
      await markSubmitRecorded(location.href);
      // APPLICATION_SAVED already advanced the queue in background (advanceAtsQueue).
      // An ATS_JOB_DONE on top popped the next pick too and PATCHed it "skipped".
      return true;
    }
    // Queue empty/mismatched — still not a skip: say what we saw.
    logBackend(`Post-apply page reached (${location.hostname}${_path}) but no queue item to record`, "warn");
    await sendMsg({ type: "ATS_JOB_DONE" });
    return true;
  }

  const SUCCESS_TEXTS = [
    "application submitted",
    "thanks for applying",
    "successfully applied",
    "application sent",
    "you've applied",
    "you have applied",
    "we've received your application",
    // High-specificity signals from Indeed's real post-apply confirmation page —
    // added after ground-truth showed the 8s window produced false negatives
    // (real submissions marked unverified because the confirmation rendered later).
    "the following items were sent",
    "your application has been submitted",
    "application has been sent",
    "has been submitted to",
    "we've sent your application",
    // ATS thank-you wording (Greenhouse / Ashby / Lever confirmation pages)
    "application received",
    "thank you for your interest",
    "thank you for applying",
    "your application to",
    "submission received",
    "we'll be in touch",
    "application complete",
    "thanks for your application",
  ];

  // or simply did nothing. Returns { verified, signal } for activity log.
  async function waitForSubmissionConfirmation(timeoutMs = 45000, opts = {}) {
    // 45s default (was 20s): in a throttled background window the post-submit thank-you
    // page renders LATE — the old 20s window marked real submits "unconfirmed" (2026-08-04).
    const start = Date.now();
    const startUrl = window.location.href;
    // opts.baselineText: page text the caller snapshotted BEFORE the triggering click.
    // A success phrase counts only if it is NOT already in the baseline — Greenhouse/
    // Lever/Ashby render the job description on the apply page itself, and its boiler-
    // plate ("thank you for your interest", "we'll be in touch") "verified" a
    // validation-blocked submit on the first beat. Callers must pass it; if one
    // doesn't, snapshot NOW — a fast confirmation may then baseline itself away and
    // come back unverified, which is the acceptable direction (applied_unconfirmed).
    // A false verified is not.
    const baseline = (opts.baselineText !== undefined
      ? opts.baselineText
      : (document.body.textContent || "")
    ).toLowerCase();

    while (Date.now() - start < timeoutMs) {
      const url = window.location.href;
      if (url !== startUrl) {
        for (const hint of POSTAPPLY_URL_HINTS) {
          if (url.toLowerCase().includes(hint)) {
            return { verified: true, signal: `url:${hint}` };
          }
        }
      }
      const bodyText = (document.body.textContent || "").toLowerCase();
      for (const phrase of SUCCESS_TEXTS) {
        if (bodyText.includes(phrase) && !baseline.includes(phrase)) {
          return { verified: true, signal: `text:${phrase.slice(0, 30)}` };
        }
      }
      // Robust fallback for ATS (opts.submitBtn passed only by phase_ats, single-submit):
      // the exact button we clicked is gone AND the page navigated → the apply form was
      // replaced by a confirmation. Survives thank-you wording differences + slow renders.
      // Guarded to >3s so it can't fire before the submit navigation happens. NOT used for
      // Indeed's multi-step SmartApply (its button legitimately changes between steps).
      if (opts.submitBtn && Date.now() - start > 3000 &&
          !opts.submitBtn.isConnected && url !== startUrl &&
          !/error|required|please (fix|complete|correct)|invalid|try again/.test(bodyText)) {
        return { verified: true, signal: "form-cleared" };
      }
      await sleep(500);
    }
    return { verified: false, signal: "timeout" };
  }

  // =========================================================================
  // React-compatible field filling
  // =========================================================================

  function setNativeValue(el, value) {
    const proto =
      el.tagName === "TEXTAREA"
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    if (setter) {
      setter.call(el, value);
    } else {
      el.value = value;
    }
    el.dispatchEvent(new Event("focus", { bubbles: true }));
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  // Type value character-by-character with random delays (50-150ms)
  async function typeValue(el, value) {
    if (!el || !value) return false;
    el.focus();
    el.dispatchEvent(new Event("focus", { bubbles: true }));
    await sleep(humanDelay(100, 200));

    // Clear existing value first
    setNativeValue(el, "");
    await sleep(humanDelay(50, 100));

    // Type each character
    for (let i = 0; i < value.length; i++) {
      const partial = value.slice(0, i + 1);
      setNativeValue(el, partial);
      await sleep(humanDelay(50, 150));
    }

    el.dispatchEvent(new Event("blur", { bubbles: true }));
    return true;
  }

  // The four fields every apply form opens with. Split out of the step loop so the rule
  // below can be DRIVEN by a test (tests/stall-needs-real-fill.test.js) instead of read.
  //
  // typeValue's RETURN VALUE is load-bearing: it is false when the profile has nothing to
  // type. The old code set `filledAny = true` next to the call regardless, so a visible
  // empty field we could never fill counted as progress — `stallRounds` was reset every
  // round and the hand-back never came. ONE empty `profile.phone` was enough to spin a form
  // until something else killed the run (candidate #4 of
  // docs/reviews/2026-09-25-delivery-honesty.md; same class as #242, where the regexp was
  // blind and the profile was empty underneath).
  //
  // `gaps` is the other half: a field we cannot fill is a fact the HUMAN can act on, so its
  // label travels into the hand-back reason instead of a bare "Continue refused".
  async function fillIdentityFields(profile, filled, gaps) {
    let any = false;
    const fields = [
      ["firstName", "first", () => profile.name || ""],
      ["lastName", "last", () => profile.last_name || ""],
      ["email", "email", () => resolveEmail(profile)],
      ["phone", "phone", () => profile.phone || ""],
    ];
    for (const [key, label, read] of fields) {
      const el = findFieldBySelectorsOrLabel(key);
      if (!el || (el.value || "").trim()) continue;
      if (await typeValue(el, await read())) {
        await sleep(humanDelay(1200, 2200));
        any = true;
        filled.push(label);
      } else {
        gaps.push(label);
      }
    }
    return any;
  }

  // Quick-set for long text (cover letters) — no char-by-char
  function quickSet(el, value) {
    if (!el || !value) return false;
    el.focus();
    setNativeValue(el, value);
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    return true;
  }

  // GLOBAL_PLAN P1c — Greenhouse/Lever location + "how did you hear" fields are react-select
  // typeaheads (confirmed: id="react-select-candidate-location-*"). A plain setNativeValue
  // leaves them UNSELECTED (no chosen option), so a REQUIRED location silently blocks submit.
  // Type into the input, wait for the options menu, and click the best-matching option (or the
  // first). Does NOT blur mid-type (blur closes the menu). Falls back to a plain fill if no
  // menu appears (so a non-react-select field can't be worse off).
  async function fillReactSelect(el, value) {
    if (!el || !value) return false;
    el.focus();
    el.dispatchEvent(new Event("focus", { bubbles: true }));
    await sleep(humanDelay(150, 300));
    setNativeValue(el, "");
    for (let i = 0; i < value.length; i++) {
      setNativeValue(el, value.slice(0, i + 1));
      await sleep(humanDelay(60, 140));
    }
    let opts = [];
    const start = Date.now();
    while (Date.now() - start < 3500) {
      opts = Array.from(document.querySelectorAll(
        '[class*="select__option"], [id*="react-select"][id*="option"], [role="option"]'
      )).filter((o) => o.offsetParent !== null && (o.textContent || "").trim());
      if (opts.length) break;
      await sleep(200);
    }
    if (!opts.length) {
      setNativeValue(el, value);
      el.dispatchEvent(new Event("blur", { bubbles: true }));
      return false; // no typeahead menu → treat as a plain input
    }
    const v = value.toLowerCase();
    const pick = opts.find((o) => (o.textContent || "").toLowerCase().includes(v)) || opts[0];
    pick.click();
    await sleep(humanDelay(300, 700));
    return true;
  }

  // A school typeahead takes only the option that IS the school, then Greenhouse's own
  // "Other" row — never fillReactSelect's first-row fallback, which is how a wrong
  // university reaches an employer. Nothing fits → the box is cleared and we report false.
  async function fillSchoolTypeahead(el, school) {
    for (const query of [school, "Other"]) {
      el.focus();
      setNativeValue(el, "");
      for (let i = 0; i < query.length; i++) {
        setNativeValue(el, query.slice(0, i + 1));
        await sleep(humanDelay(60, 140));
      }
      let opts = [];
      const start = Date.now();
      while (Date.now() - start < 3500) {
        opts = Array.from(document.querySelectorAll(
          '[class*="select__option"], [id*="react-select"][id*="option"], [role="option"]'
        )).filter((o) => o.offsetParent !== null && (o.textContent || "").trim());
        if (opts.length) break;
        await sleep(200);
      }
      const texts = opts.map((o) => (o.textContent || "").trim());
      const want = query === "Other" ? texts.find((t) => /^other$/i.test(t)) : pickSchoolOption(query, texts);
      if (want) {
        opts[texts.indexOf(want)].click();
        await sleep(humanDelay(300, 700));
        return true;
      }
    }
    setNativeValue(el, "");
    el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    return false;
  }

  // Is this field a react-select typeahead (needs option-selection, not a plain value set)?
  function isReactSelectField(el) {
    if (!el) return false;
    return (el.id && el.id.indexOf("react-select") === 0) ||
      el.getAttribute("aria-autocomplete") === "list" ||
      !!(el.closest && el.closest('[class*="select__control"]'));
  }

  // Set a native <select> value React-aware (prototype setter + change event).
  function setSelectValue(el, value) {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  // Best-effort human-readable label for any form field.
  function getFieldLabel(el) {
    const byFor = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const byGroup = el.closest("fieldset, [role='group'], [class*='question' i]")
      ?.querySelector("label, legend, [class*='label' i]");
    return (
      byFor?.textContent ||
      el.getAttribute("aria-label") ||
      byGroup?.textContent ||
      el.getAttribute("placeholder") ||
      el.name ||
      ""
    ).replace(/\s+/g, " ").trim();
  }

  // Find a visible element matching any selector in a comma-separated list
  function findVisible(selectorString) {
    for (const sel of selectorString.split(", ")) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }
    return null;
  }

  // Find element by label text
  function findByLabel(labelText) {
    const labels = formScope().querySelectorAll("label");
    for (const label of labels) {
      if (label.textContent.trim().toLowerCase().includes(labelText.toLowerCase())) {
        const forId = label.getAttribute("for");
        if (forId) {
          const input = document.getElementById(forId);
          if (input && input.offsetParent !== null) return input;
        }
        const input = label.querySelector("input, textarea, select");
        if (input && input.offsetParent !== null) return input;
      }
    }
    return null;
  }

  // =========================================================================
  // PHASE 1 — Job List Page
  // =========================================================================

  // Hardcoded fallback — kept in sync with the seed in
  // supabase-schema-v3.sql. Used if the selectors fetch fails (first run
  // before login, API down, etc) so the bot still works.
  const FALLBACK_SELECTORS = {
    jobCards: [
      ".job_seen_beacon",
      ".resultContent",
      ".jobsearch-ResultsList li",
      "[data-jk]",
      'div[class*="cardOutline"]',
      'td.resultContent',
    ],
    applyButton: [
      // 2026-09 rebuild: the button became an <a> straight to smartapply, so every
      // `button[...]` entry below silently stopped matching. Tag-free on purpose.
      '[data-testid="viewjob-indeed-apply"]',
      'button[id="indeedApplyButton"]',
      'button[data-testid="indeedApplyButton-test"]',
      'button[id*="indeedApply"]',
      ".ia-IndeedApplyButton",
      'button[class*="IndeedApply"]',
      'button[aria-label*="Apply now" i]',
      'button[aria-label*="Apply with Indeed" i]',
      'a[href*="/applystart"]',
      'button[data-testid*="apply"]',
    ],
    fields: {
      firstName: [
        'input[name*="firstName" i]',
        'input[id*="firstName" i]',
        'input[name*="first_name" i]',
        'input[id*="first_name" i]',
        'input[id="first_name"]',
        'input[autocomplete="given-name"]',
      ],
      lastName: [
        'input[name*="lastName" i]',
        'input[id*="lastName" i]',
        'input[name*="last_name" i]',
        'input[id*="last_name" i]',
        'input[id="last_name"]',
        'input[autocomplete="family-name"]',
      ],
      fullName: [
        'input[name="name"]',
        'input[id="name"]',
        'input[name*="full_name" i]',
        'input[name*="fullName" i]',
        'input[autocomplete="name"]',
        // Ashby renders a single full-name field as _systemfield_name (label "Name").
        // Without this, firstName/fullName both miss it → the app submits nameless and
        // Ashby's required-field validation blocks the submit (verified on a live form).
        'input[name="_systemfield_name"]',
        'input[id="_systemfield_name"]',
      ],
      email: [
        'input[type="email"]',
        'input[name*="email" i]',
        'input[id*="email" i]',
        'input[autocomplete="email"]',
      ],
      phone: [
        'input[type="tel"]',
        'input[name*="phone" i]',
        'input[id*="phone" i]',
        'input[autocomplete="tel"]',
      ],
      coverLetter: [
        // Greenhouse's revealed field is id="cover_letter_text" with NO name and a label
        // that reads "Enter manually" — verified on live zocdoc + affirm forms 2026-09-25.
        'textarea[id*="cover_letter" i]',
        'textarea[name*="coverletter" i]',
        'textarea[name*="cover_letter" i]',
        'textarea[name*="message" i]',
        'textarea[aria-label*="cover letter" i]',
        'textarea[id*="coverLetter" i]',
        'textarea[id*="message" i]',
      ],
    },
  };

  // Anti-detect — Phase 5.5. Patterns live in platform_selectors.detection
  // so we can update them without releasing a new extension version.
  const FALLBACK_DETECTION = {
    urlPatterns: ["captcha", "challenge", "blocked", "access-denied", "security-check", "verification"],
    // Page <title> is often the loudest signal (Cloudflare/Indeed default to
    // titles like "Security Check - Indeed.com" while body content is empty
    // until JS executes). Caught a real prod miss without this channel.
    titlePhrases: [
      "security check",
      "additional verification",
      "captcha",
      "are you a robot",
      "checking your browser",
      "just a moment",
      "please verify",
    ],
    domSelectors: [
      // reCAPTCHA V2 checkbox widget — visible challenge, not background V3
      ".g-recaptcha[data-sitekey]",
      // hCaptcha challenge iframe
      'iframe[title*="hcaptcha" i]',
      // Cloudflare interactive challenge (visible to user — not passive scripts)
      "#challenge-form",
      "#challenge-running",
      "[data-cf-challenge]",
      'div[class*="captcha-container"]',
      '[id*="datadome"]',
      // Indeed-specific: visible "verify you are human" overlay
      '[data-testid="captcha-modal"]',
      '[class*="indeed-captcha"]',
    ],
    // NOTE: cdn-cgi/challenge-platform and cdn-cgi/bm scripts are loaded on
    // ALL Indeed pages (Cloudflare passive bot management) — not a challenge
    // signal. Only flag actual third-party challenge scripts.
    scriptSrcPatterns: [
      "datadome.co",
      "perimeterx.net",
      "imperva.com",
    ],
    textPhrases: [
      "verify you are human",
      "verify that you are not a robot",
      "are you a robot",
      "unusual activity",
      "automated traffic",
      "suspicious activity",
      "access denied",
      "too many requests",
      "please confirm you are not a robot",
      "additional verification required",
      "complete the security check",
      "please enable javascript",
      "требуется дополнительная верификация",
      "подтвердите, что вы не робот",
    ],
  };

  // Loaded from backend at startup; falls back to the constants above.
  let SELECTORS = FALLBACK_SELECTORS;

  function detection() {
    return SELECTORS.detection || FALLBACK_DETECTION;
  }

  // Returns { detected: bool, signal: string } — non-empty signal points to
  // what we matched so the activity log row is debuggable. Channels checked
  // in order of cost: URL → title → DOM → script src → body text.
  function isDetected() {
    const url = window.location.href.toLowerCase();
    const det = detection();
    for (const pat of det.urlPatterns || []) {
      if (url.includes(pat)) return { detected: true, signal: `url:${pat}` };
    }
    const title = (document.title || "").toLowerCase();
    for (const phrase of det.titlePhrases || []) {
      if (title.includes(phrase)) return { detected: true, signal: `title:${phrase.slice(0, 40)}` };
    }
    for (const sel of det.domSelectors || []) {
      // querySelectorAll, not querySelector: this channel asks "is there a VISIBLE
      // challenge element", and taking only the FIRST match answered a different
      // question. A page that renders a hidden .g-recaptcha in a template before the
      // real one hid the real one behind it (2026-09-11 audit).
      const els = document.querySelectorAll(sel);
      if (!els.length) continue;
      // A present <script> matters even though it has no box of its own.
      const isScript = sel.startsWith("script[");
      if (isScript) return { detected: true, signal: `dom:${sel.slice(0, 40)}` };
      for (const el of els) {
        // NOT offsetParent: it is null for every position:fixed element — which is what a
        // modal IS. Two of this list's own selectors ([data-testid="captcha-modal"],
        // [class*="indeed-captcha"]) name fixed overlays, so the DOM channel could never
        // see them and only the title/text channels stood between us and a fake-submit
        // into a live captcha (2026-09-11 audit). isVisibleBox is also STRICTER than the
        // old test everywhere else: offsetParent is non-null for visibility:hidden.
        // 24x24 keeps out collapsed/zero-size leftovers; every real challenge widget
        // (reCAPTCHA checkbox ~300x74, hCaptcha iframe, CF interstitial) clears it easily.
        if (isVisibleBox(el, 24, 24)) {
          return { detected: true, signal: `dom:${sel.slice(0, 40)}` };
        }
      }
    }
    for (const pat of det.scriptSrcPatterns || []) {
      // NEVER treat Cloudflare's PASSIVE bot-management scripts as a challenge. cdn-cgi/
      // challenge-platform and cdn-cgi/bm load on EVERY Indeed page (see note above), so if
      // a stale/mis-set backend detection config lists them here, isDetected() would flag a
      // normal job page → runPhase's CF-JS branch waits 60s then reloads the tab → reload
      // nukes the content script → re-detect → loop forever, phase2 never runs (live
      // 2026-07-28: pool stalled on a clean /viewjob, re-init every ~63s, 0 applies). Skip.
      if (pat.includes("cdn-cgi")) continue;
      const scripts = document.querySelectorAll("script[src]");
      for (const s of scripts) {
        if ((s.src || "").toLowerCase().includes(pat)) {
          return { detected: true, signal: `script:${pat}` };
        }
      }
    }
    const bodyText = (document.body?.textContent || "").toLowerCase();
    for (const phrase of det.textPhrases || []) {
      if (bodyText.includes(phrase)) return { detected: true, signal: `text:${phrase.slice(0, 40)}` };
    }
    return { detected: false, signal: "" };
  }

  // A consent wall is NOT a challenge. Nothing here is testing whether we are human —
  // the site is asking the ACCOUNT HOLDER to agree to something ("we've updated our
  // Terms", "Accept to continue"), and until they do, every job page behind it is
  // blocked. We never click Accept for them: agreeing to a ToS is a legal act by the
  // person whose account it is, and a bot-accepted agreement is exactly the kind of
  // thing that voids one. So we do what we do with a captcha — pause, name the button,
  // and resume the moment it's gone.
  const FALLBACK_CONSENT_GATE = {
    // The dialog has to SAY it is about terms…
    phrases: [
      "terms of service",
      "terms and conditions",
      "terms of use",
      "user agreement",
      "updated our terms",
      "updated terms",
      "accept the terms",
      "accept terms",
      // "privacy policy" is deliberately NOT here. It appears in every cookie banner and
      // in most ATS footers, and a cookie banner is usually dismissible rather than
      // blocking — pausing a campaign behind one would trade a rare stall for a constant
      // one. A genuine consent wall names the terms.
    ],
    // …and carry a button that accepts them. "Continue" / "OK" alone is far too generic
    // — half the modals on a job board have one, and pausing on those would stall every
    // run behind a dismissible promo.
    acceptLabels: [
      "accept",
      "agree",
      "i agree",
      "i accept",
      "accept all",
      "accept and continue",
      "accept & continue",
      "agree and continue",
      "agree & continue",
    ],
  };

  function consentGate() {
    return detection().consentGate || FALLBACK_CONSENT_GATE;
  }

  // offsetParent is null for position:fixed elements — which is what every modal is —
  // so the offsetParent test used elsewhere in this file cannot be reused here.
  function isVisibleBox(el, minW, minH) {
    const r = el.getBoundingClientRect();
    if (r.width < minW || r.height < minH) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === "hidden" || cs.display === "none") return false;
    return parseFloat(cs.opacity || "1") > 0.1;
  }

  // Returns { gated: bool, label: string }. `label` is the exact button text so the
  // hand-off can tell the user which word to look for instead of "accept the terms",
  // which may not be what the button says.
  function detectConsentGate() {
    const cfg = consentGate();
    const phrases = cfg.phrases || [];
    const accepts = cfg.acceptLabels || [];
    const dialogs = document.querySelectorAll('[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open]');
    for (const d of dialogs) {
      // A real consent wall covers the page. 240x100 keeps out the collapsed/aria-only
      // dialog nodes that React frameworks leave in the DOM permanently.
      if (!isVisibleBox(d, 240, 100)) continue;
      const text = (d.textContent || "").toLowerCase();
      if (!phrases.some((p) => text.includes(p))) continue;
      // An apply or login modal quotes the terms in its fine print ("by continuing you
      // agree to…"). Those are FORMS — the campaign's job is to fill them, not to park
      // in front of them. If the dialog collects input, it is not a consent wall.
      // (This is also what keeps the Lever/Greenhouse "I agree to the terms" checkbox,
      // which lives inside the application form, from stopping every ATS submit.)
      if (d.querySelector('input[type="file"], input[type="password"], textarea')) continue;
      const typed = d.querySelectorAll(
        'input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]):not([type="submit"]):not([type="button"])'
      );
      if (typed.length > 1) continue;
      const buttons = d.querySelectorAll('button, a[role="button"], [role="button"], input[type="submit"], input[type="button"]');
      for (const btn of buttons) {
        const raw = (btn.innerText || btn.textContent || btn.value || "").trim().replace(/\s+/g, " ");
        if (!raw || raw.length > 40) continue;
        const norm = raw.toLowerCase();
        if (accepts.some((a) => norm === a || norm.startsWith(a + " "))) {
          return { gated: true, label: raw };
        }
      }
    }
    return { gated: false, label: "" };
  }

  async function loadSelectors() {
    const platform = detectPlatform();
    const cacheKey = `selectors_${platform}`;
    try {
      const cached = await storageGet([cacheKey, `${cacheKey}_at`]);
      const fresh = cached[`${cacheKey}_at`] && Date.now() - cached[`${cacheKey}_at`] < 24 * 3600 * 1000;
      if (fresh && cached[cacheKey]) {
        SELECTORS = cached[cacheKey];
        return;
      }
      const resp = await sendMsg({ type: "GET_SELECTORS", platform });
      if (resp?.selectors) {
        SELECTORS = resp.selectors;
        await storageSet({
          [cacheKey]: resp.selectors,
          [`${cacheKey}_at`]: Date.now(),
        });
      }
    } catch {
      // keep FALLBACK_SELECTORS
    }
  }

  function findJobCards() {
    for (const sel of SELECTORS.jobCards || FALLBACK_SELECTORS.jobCards) {
      const cards = document.querySelectorAll(sel);
      if (cards.length > 0) return Array.from(cards);
    }
    return [];
  }

  function isEasilyApplyCard(card) {
    const text = card.textContent || "";
    return /easily\s*apply/i.test(text);
  }

  function extractCardInfo(card) {
    // The named selectors are the old SERP DOM. The last two are the invariant: however
    // Indeed redraws the card, it must link the posting (viewjob/jk) or the card is
    // useless to Indeed itself. Without them a SERP redesign zeroes every card via the
    // `!info.title || !info.clickEl` gate below and the walk pages forever through
    // "No Easy Apply jobs" — the exact silent shape the /viewjob rebuild had (2026-09-11).
    const titleEl =
      card.querySelector(".jobTitle a") ||
      card.querySelector("h2.jobTitle a") ||
      card.querySelector("h2 a") ||
      card.querySelector("a[data-jk]") ||
      card.querySelector('a[href*="viewjob"]') ||
      card.querySelector('a[href*="jk="]');
    const companyEl =
      card.querySelector('[data-testid="company-name"]') ||
      card.querySelector(".companyName") ||
      card.querySelector('[class*="company"]');

    const title = titleEl?.textContent?.trim() || "";
    const company = companyEl?.textContent?.trim() || "";
    // The card's place line ("Remote in San Francisco, CA", captured 10-05,
    // tests/fixtures/indeed-serp-decoy.html). Never harvested before: 0 of 344 Indeed pool
    // rows had a location, so the deck's city filter passed every Indeed job as "unknown".
    const location = (
      card.querySelector('[data-testid="text-location"]') ||
      card.querySelector(".companyLocation")
    )?.textContent?.replace(/\s+/g, " ").trim() || "";
    const href = titleEl?.getAttribute("href") || "";
    const url = href.startsWith("http") ? href : "https://www.indeed.com" + href;
    let jk = card.getAttribute("data-jk") || titleEl?.getAttribute("data-jk") || "";
    if (!jk && href) {
      const m = href.match(/[?&]jk=([a-z0-9]+)/i);
      if (m) jk = m[1];
    }

    // The card's own blurb. Thin — a sentence or two — but it is the ONLY description
    // that exists before we open the posting, and the deck has to rank the pool before
    // the user swipes it. Harvesting it costs nothing: the text is already in this DOM,
    // no extra page load, so no extra ban surface. Without it every Indeed row landed
    // with score null and sorted last forever (a third of the pool).
    const snippetEl =
      card.querySelector(".job-snippet") ||
      card.querySelector('[class*="jobSnippet"]') ||
      card.querySelector('[data-testid="jobsnippet_footer"]') ||
      card.querySelector("ul");
    const snippet = (snippetEl?.textContent || "").replace(/\s+/g, " ").trim().slice(0, 1500);

    return { title, company, location, url, jk, snippet, clickEl: titleEl };
  }

  // Indeed plants decoy cards in the SERP (captured live 2026-10-05, fixture
  // tests/fixtures/indeed-serp-decoy.html): a clone of the card above it with a made-up jk
  // (a1b2c3d4e5f67890, fedcba9876543210, 0f1e2d3c4b5a6978, …), aria-hidden, tabindex -1,
  // 0 px tall, linking /viewjob instead of /rc/clk. No person can see or tab to one; a script
  // walking [data-jk] opens it. That cost 4 of 11 opens in the 10-06 run as "Dead link" — each
  // a bot signal — and harvested every first-seen decoy into the pool under a real title.
  // Recognised by what a person sees, never by the jk: the values rotate.
  function isDecoyCard(info) {
    const a = info.clickEl;
    if (!a) return false;
    if (a.closest('[aria-hidden="true"]')) return true;
    // The clone keeps the original's title span id (jobTitle-<real jk>) under its fake jk.
    const span = a.querySelector('[id^="jobTitle-"]');
    const spanJk = span ? span.id.slice("jobTitle-".length) : "";
    return !!(info.jk && spanJk && spanJk !== info.jk);
  }

  async function phase1_jobList() {
    const platform = detectPlatform();
    if (platform === "ziprecruiter") return await phase1_ziprecruiter();
    return await phase1_indeed();
  }

  async function phase1_indeed() {
    if (!(await isCampaignRunning())) return;

    // Before anything reads this SERP (the empty-q guard, the scan, the pool harvest): the
    // first search of a board is typed, and may have come back in the wrong city.
    if (await correctFirstIndeedSearch()) return;

    // Guard: if Indeed redirected us to a generic q= page (e.g. from an expired
    // viewjob that auto-redirects), mark the job that caused the redirect as
    // processed and skip to the next pending job. Using skipToNextJob() (not
    // goBackToJobList) preserves the remaining jobs from the current page scan.
    const urlQ = new URL(window.location.href).searchParams.get("q") || "";
    const filtersData = await storageGet(["campaignFilters", "pendingJobs", "currentJobIndex"]);
    const kw = filtersData.campaignFilters?.keywords || [];
    if (kw.length && !urlQ.trim()) {
      log("Redirected to empty search — marking failed job and skipping...", "");
      const jobs = filtersData.pendingJobs || [];
      const idx = filtersData.currentJobIndex || 0;
      const failedJob = jobs[idx];
      if (failedJob?.jk) {
        const seen = await storageGet("processedJobKeys");
        const keys = seen.processedJobKeys || [];
        if (!keys.includes(failedJob.jk)) {
          await storageSet({ processedJobKeys: [...keys, failedJob.jk].slice(-500) });
        }
      }
      await skipToNextJob();
      return;
    }

    const count = await getPlatformCount("indeed");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`Indeed daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}) — trying another platform.`, "ok");
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "indeed", reason: "platform daily cap" });
      return;
    }

    log("Scanning job list for Easy Apply jobs...", "");
    logBackend("Scanning job list for Easy Apply postings…", "info");

    // Wait for cards to load
    await sleep(humanDelay(2000, 3000));

    const cards = findJobCards();
    if (!cards.length) {
      log("No job cards found on page", "err");
      return;
    }

    // Filter for "Easily apply" jobs
    const candidates = [];
    const alreadyApplied = await getAppliedUrls();
    const seenKeys = await storageGet("processedJobKeys");
    const processedKeys = new Set(seenKeys.processedJobKeys || []);

    const decoys = [];
    for (const card of cards) {
      if (!isEasilyApplyCard(card)) continue;
      const info = extractCardInfo(card);
      if (!info.title || !info.clickEl) continue;
      if (isDecoyCard(info)) { decoys.push(info.jk || "?"); continue; }
      if (alreadyApplied.has(info.url)) continue;
      if (info.jk && processedKeys.has(info.jk)) continue;
      candidates.push(info);
    }
    if (decoys.length) logBackend(`🪤 skipped ${decoys.length} decoy card(s) jk=[${decoys.join(",")}]`, "info");

    // HARVEST-TO-POOL (Igor 2026-07-27): the tap deck needs Indeed/ZR inventory, and the
    // server deliberately never scrapes Indeed (compliant-by-design). So every Easy Apply
    // card this browser SEES is saved to the job pool — canonical /viewjob?jk= link so the
    // pool identity matches the by-link executor and the dedup lane. Fire-and-forget:
    // a slow backend must never stall the walk. Server skips already-known links.
    // Harvested BEFORE the title gate below: the pool is a shared crawl index, and a title
    // that is off-target for THIS user's roles is another user's match.
    try {
      const _plat = detectPlatform();
      const harvest = candidates
        .map((j) => ({
          title: j.title || "",
          company: j.company || "",
          link: j.jk ? `https://www.indeed.com/viewjob?jk=${j.jk}` : (j.url || ""),
          platform: _plat,
          location: j.location || "",
          // Server scores rows that arrive with a description (>=120 chars) and leaves
          // title-only ones null — see /jobs/ingest. Sending "" is the same as sending
          // nothing, so a card without a snippet degrades to the old behaviour.
          description: j.snippet || "",
        }))
        .filter((j) => j.link && j.title);
      if (harvest.length) {
        Promise.resolve(sendMsg({ type: "INGEST_JOBS", data: { jobs: harvest } })).catch(() => {});
      }
    } catch (_) { /* harvest is best-effort */ }

    // Title gate on the CARD, before anything is opened (same rule as the detail phase).
    let { keep: easyApplyCards, skipped: offTitle } =
      splitCardsByTitle(candidates, await titleGateKeywords());
    if (offTitle.length) logBackend(titleSkipSummary(offTitle, candidates.length), "info");

    // A page whose every card failed the title gate moves on exactly like an empty page:
    // same nav (rotate to the next phrase/lap), and the phrase is NOT retired — it had
    // results, just none for these roles on this page.
    if (!easyApplyCards.length) {
      log("No new Easy Apply jobs found. Checking next page...", "");
      logBackend(candidates.length
        ? "None of this page's Easy Apply jobs match your roles — going to next"
        : "No Easy Apply jobs on this page — going to next", "info");
      await goToNextPage();
      return;
    }

    // Judge the whole page before opening anything (auto only: a pool run is the person's
    // own pick, and tap mode makes the person the filter — neither runs the fit gate).
    {
      const st = await storageGet(["reviewMode", "atsPlatform"]);
      if (st.reviewMode !== true && st.atsPlatform !== "pool") {
        const judged = await prejudgeIndeedCards(easyApplyCards);
        if (!(await isCampaignRunning())) return;
        if (judged && judged.platformDone) {
          await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "indeed", reason: "broad mode daily cap" });
          return;
        }
        if (judged) easyApplyCards = judged;
      }
    }
    if (!easyApplyCards.length) {
      log("Nothing on this page fits you. Checking next page...", "");
      logBackend("None of this page's postings fit you — going to next", "info");
      await goToNextPage();
      return;
    }

    log(`Found ${easyApplyCards.length} Easy Apply jobs`, "ok");
    logBackend(`Found ${easyApplyCards.length} Easy Apply jobs on page`, "ok");

    // Save pending jobs
    await storageSet({
      pendingJobs: easyApplyCards.map((j) => ({
        title: j.title,
        company: j.company,
        location: j.location || "",
        url: j.url,
        jk: j.jk,
        // The pool row the search-page judge stored this posting's verdict on.
        ...(j.job_id ? { job_id: j.job_id } : {}),
      })),
      currentJobIndex: 0,
    });

    // Navigate directly to /viewjob?jk=xxx rather than SPA-clicking the card.
    // Clicking a card keeps the URL on /jobs?...&vjk=xxx which detectPhase()
    // now correctly treats as "list", causing phase1 to re-run in a loop.
    // A full-page navigation to /viewjob produces a clean "detail" URL.
    const firstJob = easyApplyCards[0];
    log(`Opening: ${firstJob.title} @ ${firstJob.company}`, "");
    logBackend(`Opening job: ${firstJob.title} @ ${firstJob.company}`, "info");
    await sleep(humanDelay(3000, 7000));
    if (!(await isCampaignRunning())) return;
    const viewjobUrl = firstJob.jk
      ? `https://www.indeed.com/viewjob?jk=${firstJob.jk}`
      : firstJob.url;
    window.location.href = viewjobUrl;
  }

  async function getAppliedUrls() {
    const data = await storageGet("appliedUrls");
    return new Set(data.appliedUrls || []);
  }

  // The cached profile has no email (it lives in Supabase auth). Fall back to the
  // stored JWT's `email` claim so ATS forms with a blank email field get filled.
  async function resolveEmail(profile) {
    if (profile && profile.email) return profile.email;
    try {
      const { supabase_token } = await storageGet("supabase_token");
      if (supabase_token) {
        const p = JSON.parse(atob(supabase_token.split(".")[1]));
        if (p && p.email) return p.email;
      }
    } catch { /* no/broken token */ }
    return "";
  }

  async function addAppliedUrl(url) {
    const data = await storageGet("appliedUrls");
    const urls = data.appliedUrls || [];
    urls.push(url);
    // Keep last 500
    if (urls.length > 500) urls.splice(0, urls.length - 500);
    await storageSet({ appliedUrls: urls });
  }

  // A URL-independent dedup key. Board job URLs carry volatile params (ZR's lk=,
  // tracking) so the same posting can present different URLs across runs. Keying by
  // title|company catches the same job regardless — the robust cross-session guard.
  function jobDedupKey(title, company) {
    return `${(title || "").toLowerCase().replace(/\s+/g, " ").trim()}|${(company || "").toLowerCase().replace(/\s+/g, " ").trim()}`;
  }

  async function getAppliedJobKeys() {
    const data = await storageGet("appliedJobKeys");
    return new Set(data.appliedJobKeys || []);
  }

  // Jobs we already handed back TODAY. A hand-back is terminal for this run, but the
  // walk re-encounters the same postings on the next pass through the results and used
  // to re-run the fit judge and the cover letter on each of them — live 08-15 the same
  // two jobs were paid for three times in twenty minutes, and they crowded out the jobs
  // further down the list that we CAN submit. Keyed by day so tomorrow is a clean slate
  // (the blocker may be gone, e.g. after the user fills in a missing profile field).
  // Same day, same clock as recordLocalApplication / getPlatformCount — see the note in
  // background.js. Mixing local and UTC on one storage key silently reset the caps.
  function localDay() {
    return new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in the user's timezone
  }

  async function getHandedBackKeys() {
    const d = await storageGet(["handedBackKeys", "handedBackDate"]);
    const today = localDay();
    if (d.handedBackDate !== today) return new Set();
    return new Set(d.handedBackKeys || []);
  }

  async function addHandedBackKey(title, company) {
    const key = jobDedupKey(title, company);
    if (!key || key === "|") return;
    const today = localDay();
    const d = await storageGet(["handedBackKeys", "handedBackDate"]);
    const keys = d.handedBackDate === today ? (d.handedBackKeys || []) : [];
    if (!keys.includes(key)) keys.push(key);
    if (keys.length > 500) keys.splice(0, keys.length - 500);
    await storageSet({ handedBackKeys: keys, handedBackDate: today });
  }

  async function addAppliedJobKey(title, company) {
    const key = jobDedupKey(title, company);
    if (!key || key === "|") return;
    const data = await storageGet("appliedJobKeys");
    const keys = data.appliedJobKeys || [];
    if (!keys.includes(key)) keys.push(key);
    if (keys.length > 1000) keys.splice(0, keys.length - 1000);
    await storageSet({ appliedJobKeys: keys });
  }

  // Increment the local application count from the CONTENT SCRIPT (not the service
  // worker). MV3 service workers can run stale code after an extension reload, which
  // left the count at 0 despite real submissions. content.js reloads reliably on
  // navigation, so counting here makes the daily cap + count robust regardless of SW
  // state. background's APPLICATION_SAVED no longer increments (backend save only).
  async function recordLocalApplication(platform) {
    // One read for everything, so two tabs recording at once race over one round-trip.
    const s = await storageGet([
      "todayCount", "platformCounts", "todayDate", "atsPlatform",
      "keywordCounts", "campaignFilters", "kwIndex",
    ]);
    const today = localDay();
    const totalCount = (s.todayDate === today ? (s.todayCount || 0) : 0) + 1;
    const platformCounts = s.todayDate === today ? (s.platformCounts || {}) : {};
    platformCounts[platform] = (platformCounts[platform] || 0) + 1;
    // Per-keyword ledger behind keywordSubCap: which search phrase this application came
    // out of. Only the LIVE board search has a phrase — a pool/ATS queue walk (atsPlatform
    // set) applies to saved rows, and charging the current phrase for those would rotate
    // the search away from a keyword that never spent anything.
    const keywordCounts = ledgerOf(s.keywordCounts);
    if (!s.atsPlatform) {
      const key = keywordKeyOf(s);
      if (key) {
        const bucket = keywordCounts[platform] || (keywordCounts[platform] = {});
        bucket[key] = (bucket[key] || 0) + 1;
      }
    }
    await storageSet({ todayCount: totalCount, platformCounts, keywordCounts, todayDate: today });
    return platformCounts[platform];
  }

  // Inverse of recordLocalApplication — used when a submit we optimistically counted
  // (recorded BEFORE the click, nav-safe) turns out to be BLOCKED by form validation:
  // claiming "applied" for an application that never left the page is lying to the user
  // (council 2026-08-04: quality above all — no silent half-deaths).
  async function subtractLocalApplication(platform) {
    const s = await storageGet([
      "todayCount", "platformCounts", "todayDate", "atsPlatform",
      "keywordCounts", "campaignFilters", "kwIndex",
    ]);
    const today = localDay();
    if (s.todayDate !== today) return;
    const platformCounts = s.platformCounts || {};
    platformCounts[platform] = Math.max(0, (platformCounts[platform] || 0) - 1);
    // Give the phrase its slot back too, or a blocked submit would quietly shrink this
    // keyword's share of the cap for the rest of the day.
    const keywordCounts = ledgerOf(s.keywordCounts);
    const key = keywordKeyOf(s);
    if (!s.atsPlatform && key && keywordCounts[platform]) {
      keywordCounts[platform][key] = Math.max(0, (keywordCounts[platform][key] || 0) - 1);
    }
    await storageSet({
      todayCount: Math.max(0, (s.todayCount || 0) - 1),
      platformCounts,
      keywordCounts,
    });
  }

  // Council 2026-08-04 "frequency ledger": every field the filler could NOT complete,
  // by label — this is the instrumentation that drives which deterministic handlers to
  // build next (EEO/location/etc). Read via HIREDROP_READ_STORAGE keys:["unfilledLedger"].
  // ── Title relevance (one rule, one place; fixtures in tests/title-match.test.js) ──
  //
  // The cheap gate before the paid ones: a title sharing NO word with any keyword is
  // skipped before the fit judge and the cover letter are ever called. It exists because
  // the ATS pool is shared across users — a search for "event manager" walks past
  // welders and jewellers, and judging those costs real money.
  //
  // It compared EXACT words, so "event" never matched "Events" and the gate threw away
  // the most relevant listings it saw: measured on Igor's 09-20 run, "Director of Special
  // Events" and "Special Events Assistant" were both dropped unread under the keyword
  // "event manager". A skip here is invisible in a way a fit-skip is not — the judge
  // never scored it, so nothing in the log says a good job was passed over.
  //
  // Singular/plural is now one word. Deliberately nothing more: no synonyms, no stemming
  // of "marketing" to "market". "Conference Planner" still doesn't match "event manager",
  // and that is the honest answer for a WORD filter — semantic judgement is the judge's
  // job, and it has the whole posting to work with, not three words of a heading.
  function titleStem(w) {
    // Short words are left alone: "ops"/"op" and "hr" are not plurals of anything.
    if (w.length < 5) return w;
    if (w.endsWith("ies")) return w.slice(0, -3) + "y";   // strategies → strategy
    if (w.endsWith("ses") || w.endsWith("xes") || w.endsWith("ches") || w.endsWith("shes")) return w.slice(0, -2);
    if (w.endsWith("s") && !w.endsWith("ss")) return w.slice(0, -1); // events → event
    return w;
  }

  function titleMatchesKeywords(title, keywords) {
    const words = (t) => new Set(
      String(t || "").toLowerCase().split(/\W+/).filter((w) => w.length > 2).map(titleStem)
    );
    const titleWords = words(title);
    const keywordWords = new Set(
      (keywords || []).flatMap((phrase) => [...words(phrase)])
    );
    if (!keywordWords.size) return true; // no keywords = no filter, same as at harvest
    return [...keywordWords].some((w) => titleWords.has(w));
  }

  // The keywords the gate checks against. A pool swipe run gets NONE — the user
  // hand-picked that job, and a word filter must never veto their pick. The list phase
  // (card titles) and the detail phase (the opened posting) both read through this, so the
  // two can never disagree about which keywords or which exception apply.
  async function titleGateKeywords() {
    if ((await storageGet("atsPlatform")).atsPlatform === "pool") return [];
    return ((await storageGet("campaignFilters")).campaignFilters?.keywords || []).filter(Boolean);
  }

  // List phase: the same rule, on the card title, BEFORE a card is opened. Every card the
  // detail gate would reject used to cost a full job-page load first — Igor's 10-05 run
  // opened 26 postings only to skip them on title, against 11 applications, and each open
  // is time on the walk plus one more page view Indeed's bot detection gets to look at.
  // The detail-phase check stays as the backstop: a card title can be truncated.
  function splitCardsByTitle(cards, keywords) {
    const keep = [];
    const skipped = [];
    for (const c of cards || []) (titleMatchesKeywords(c.title, keywords) ? keep : skipped).push(c);
    return { keep, skipped };
  }

  // ONE activity-log line per results page, not one per card — the backend log is the
  // user's feed. A few titles ride along so a wrong skip is still visible (a gate that
  // drops jobs unread must not drop them silently).
  function titleSkipSummary(skipped, total) {
    if (!skipped.length) return "";
    const eg = skipped.slice(0, 3).map((c) => `"${String(c.title || "").slice(0, 60)}"`).join(", ");
    return `Skipped ${skipped.length} of ${total} cards: title doesn't match your roles (e.g. ${eg})`;
  }

  // ── Search-page judge ──────────────────────────────────────────────────
  // The walk used to open every card and judge it on its page, one at a time: ~18 s per
  // rejected posting (10-07: 18 rejections in a row, 0 applied, then Indeed ran dry). Now
  // the cards are read where a person reads them — click a card, the results page shows
  // the posting in its right-hand pane (Indeed's own request, no page load; live 10-07 in
  // Igor's Chrome: 0.5-1.5 s per posting, 3-13k chars) — and sent to the server in chunks
  // while the next ones are read (/tools/assess-fit-batch): judged in parallel, every
  // verdict stored, a posting judged on an earlier run answered from memory. Only the ones
  // that fit are opened.
  //
  // Not /rpc/jobdescs: it answers (30 postings in 0.36 s) but Indeed's own page never calls
  // it (checked 10-07: the pane loads through apis.indeed.com/graphql), so every results
  // page would carry a request no person's browser makes.
  //
  // Any failure returns null and the walk judges each posting on its page exactly as
  // before — the speed-up may be lost, never an application.
  //
  // The pane, live 10-07 (1400 px window): #jobsearch-ViewjobPaneWrapper inside
  // .jobsearch-RightPane; the posting's title is [data-testid="vj-job-title"], its text
  // .simple-job-description-html. What a click does there, polled every 30-40 ms: vjk
  // follows the card within 5 ms while the pane still shows the PREVIOUS posting, at
  // ~60 ms the pane empties, at ~1.4 s the new title and text arrive together. So a
  // posting is credited to a card only when the pane's own title names that card —
  // vjk alone would hand posting A's text (and a stored skip) to card B.
  const PANE_SELECTOR = "#jobsearch-ViewjobPaneWrapper, .jobsearch-RightPane";
  const PANE_DESC_SELECTOR = ".simple-job-description-html, #jobDescriptionText";
  const PANE_TITLE_SELECTOR = '[data-testid="vj-job-title"], [data-testid="jobsearch-JobInfoHeader-title"]';
  // Below this it is a card snippet, not a posting (the server's floor is the same).
  const PREJUDGE_MIN_TEXT = 300;
  const PREJUDGE_CHUNK = 5;
  // This many cards in a row the pane wouldn't show = it isn't working on this page.
  const PREJUDGE_MAX_UNREAD_RUN = 3;

  function resultsPane() {
    return document.querySelector(PANE_SELECTOR);
  }

  // The pane is only there to read when it is drawn. In a narrow window (live 10-07:
  // 628 px) Indeed keeps it in the DOM, posting and all, but at display:none — and a
  // card click there navigates to /viewjob mid-walk.
  function paneShown() {
    const p = resultsPane();
    if (!p) return false;
    const r = p.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  function paneTitle() {
    const el = resultsPane()?.querySelector(PANE_TITLE_SELECTOR);
    return el ? String(el.textContent || "").replace(/\s+/g, " ").trim() : "";
  }

  const normTitle = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  function paneNames(card) {
    const t = normTitle(paneTitle());
    return !!t && t === normTitle(card.title);
  }

  function paneVjk() {
    return new URL(window.location.href).searchParams.get("vjk");
  }

  function paneText() {
    const el = resultsPane()?.querySelector(PANE_DESC_SELECTOR);
    if (!el) return "";
    // innerText: the rendered text, line breaks kept, the pane's own <style> left out.
    let raw = typeof el.innerText === "string" ? el.innerText : "";
    if (!raw) {
      const c = el.cloneNode(true);
      c.querySelectorAll("style,script").forEach((n) => n.remove());
      raw = c.textContent || "";
    }
    return String(raw)
      .replace(/[ \t ]+/g, " ")
      .replace(/ *\n\s*/g, "\n")
      .trim()
      .slice(0, 8000);
  }

  // The pane already shows this card: Indeed opens the page with its first card in the
  // pane (vjk = that card, or no vjk yet while the page is still settling). Clicking it
  // again redraws nothing, so waiting for a change would lose it every time.
  function paneAlreadyShows(card, cards) {
    if (!paneNames(card)) return false;
    const vjk = paneVjk();
    if (vjk) return vjk === card.jk;
    // No vjk: the title is the only witness, so it must name no other card on the page.
    return cards.filter((c) => normTitle(c.title) === normTitle(card.title)).length === 1;
  }

  // Click the card and wait for the pane to show THAT posting: vjk is its jk, the pane
  // went through a redraw after the click (emptied or changed — not the previous posting
  // still standing), its title names the card, and the text reads the same on two polls
  // in a row (not half-drawn).
  async function readCardInPane(card, cards, timeoutMs = 5000) {
    if (paneAlreadyShows(card, cards)) {
      const text = paneText();
      if (text.length >= PREJUDGE_MIN_TEXT) return text;
    }
    const beforeText = paneText();
    const beforeTitle = paneTitle();
    try { card.clickEl.scrollIntoView({ block: "center", behavior: "smooth" }); } catch (_) {}
    await sleep(humanDelay(250, 700));
    await humanClick(card.clickEl);
    const until = Date.now() + timeoutMs;
    let redrawn = false;
    let last = "";
    while (Date.now() < until) {
      await sleep(120);
      if (paneVjk() !== card.jk) continue;
      const text = paneText();
      if (!redrawn && (text !== beforeText || paneTitle() !== beforeTitle)) redrawn = true;
      if (redrawn && paneNames(card) && text.length >= PREJUDGE_MIN_TEXT) {
        if (text === last) return text;
        last = text;
      } else {
        last = "";
      }
    }
    return "";
  }

  // After a card the pane never showed, its posting may still be on the way. Let it land
  // (nothing changes for 1.5 s, at most 6 s) before the next click, so it can't arrive
  // while the next card is being read.
  async function paneSettle(quietMs = 1500, maxMs = 6000) {
    const until = Date.now() + maxMs;
    let sig = `${paneTitle()}\n${paneText()}`;
    let since = Date.now();
    while (Date.now() < until) {
      await sleep(150);
      const now = `${paneTitle()}\n${paneText()}`;
      if (now !== sig) {
        sig = now;
        since = Date.now();
      } else if (Date.now() - since >= quietMs) {
        return;
      }
    }
  }

  function sendCardsToJudge(cards, texts) {
    return sendMsg(
      {
        type: "PREJUDGE_CARDS",
        data: {
          jobs: cards.map((c) => ({
            title: c.title || "",
            company: c.company || "",
            location: c.location || "",
            platform: "indeed",
            link: `https://www.indeed.com/viewjob?jk=${c.jk}`,
            description: texts[c.jk],
          })),
        },
      },
      35000
    );
  }

  // -> the cards to open, in page order (a passed card carries the job_id its verdict is
  // stored under), { platformDone } when Broad's daily cap is spent, or null = judge every
  // card on its page as before.
  async function prejudgeIndeedCards(pageCards) {
    const t0 = Date.now();
    // The same posting twice on one page is read and judged once (a second copy used to
    // log a second "Skipped (fit" line and count twice as a fit loss).
    const cards = pageCards.filter((c, i) => !c.jk || pageCards.findIndex((x) => x.jk === c.jk) === i);
    // A hidden tab (window minimised or covered) answers no card click at all — live 10-07
    // the pane stood still until the window was raised.
    if (document.visibilityState === "hidden") {
      logBackend("Search-page judge: the automation window is hidden — checking each posting on its page", "warn");
      return null;
    }
    // The page may still be drawing its results; give the pane a moment to appear.
    for (let i = 0; i < 12 && !paneShown(); i++) await sleep(250);
    if (!paneShown()) {
      logBackend("Search-page judge: no results pane (window too narrow?) — checking each posting on its page", "warn");
      return null;
    }
    const texts = {};
    const inFlight = [];
    let chunk = [];
    let unread = 0;
    let unreadRun = 0;
    let seen = 0;
    // Titles of cards the pane never showed. Their posting may still land later, during
    // another card's read — and the title check can't tell two postings with one title
    // apart (common on a page). So a card sharing such a title goes to its own page.
    const unreadTitles = new Set();
    for (const c of cards) {
      seen++;
      if (!(await isCampaignRunning())) return null;
      // Mid-walk the window can shrink or the page can move on: a click with no pane
      // navigates, so stop clicking.
      if (!paneShown() || /^\/(viewjob|rc\/clk|pagead\/)/.test(new URL(window.location.href).pathname)) {
        logBackend("Search-page judge: the results pane went away — checking each posting on its page", "warn");
        return null;
      }
      if (unreadTitles.has(normTitle(c.title))) {
        unread++;
        continue;
      }
      const text = c.jk && c.clickEl ? await readCardInPane(c, cards) : "";
      if (text.length >= PREJUDGE_MIN_TEXT) {
        texts[c.jk] = text;
        chunk.push(c);
        unreadRun = 0;
      } else {
        unread++;
        unreadTitles.add(normTitle(c.title));
        if (++unreadRun >= PREJUDGE_MAX_UNREAD_RUN) {
          logBackend(`Search-page judge: the pane showed none of ${unreadRun} postings in a row — checking each posting on its page`, "warn");
          return null;
        }
        await paneSettle();
      }
      if (chunk.length >= PREJUDGE_CHUNK) {
        inFlight.push(sendCardsToJudge(chunk, texts));
        chunk = [];
        // A page takes a minute or more at a person's pace; a silent minute reads as a
        // stall to the feed and to drive.py (180 s of silence = stuck).
        logBackend(`Search-page judge: read ${seen} of ${cards.length} postings on this page…`, "info");
      }
      // A person's pace, not a scraper's: ~4 s a card (~1 min for a page of 15). The
      // judge's answers come back while the next cards are read, so it costs no wait.
      await sleep(humanDelay(2500, 7000));
    }
    if (chunk.length) inFlight.push(sendCardsToJudge(chunk, texts));
    if (!inFlight.length) {
      logBackend("Search-page judge: no posting text in the results pane — checking each posting on its page", "warn");
      return null;
    }
    const answers = await Promise.all(inFlight);
    // The judge answers seconds later; a Stop in between must not log verdicts for a run
    // that is over (the caller re-checks too, but only after this function has logged).
    if (!(await isCampaignRunning())) return null;
    const results = [];
    let reused = 0;
    let lost = 0;
    for (const a of answers) {
      if (a && Array.isArray(a.results)) {
        results.push(...a.results);
        reused += a.reused || 0;
      } else {
        lost++;
      }
    }
    if (lost === answers.length) {
      logBackend("Search-page judge unavailable — checking each posting on its page", "warn");
      return null;
    }
    const verdicts = new Map(results.map((v) => [v.link, v]));
    // Broad mode's daily cap is spent: every card on every page would come back skipped, so
    // say so once and hand the walk on, instead of paging through the rest of the search.
    if (results.length && results.every((v) => v.source === "broad_cap")) {
      logBackend(`Broad mode daily limit reached — ${String(results[0].reason || "").slice(0, 120)}`, "ok");
      return { platformDone: true };
    }
    const keep = [];
    const skipped = [];
    for (const c of cards) {
      const v = c.jk ? verdicts.get(`https://www.indeed.com/viewjob?jk=${c.jk}`) : null;
      if (v && v.decision === "skip") skipped.push({ c, v });
      else if (v && v.decision === "apply" && v.job_id) keep.push({ ...c, job_id: v.job_id, fit: true });
      // Unjudged (or a chunk that got no answer): the job page decides. Its job_id still
      // rides along — a verdict that lands after the server's deadline is stored, and the
      // job page then reuses it.
      else keep.push(v && v.job_id ? { ...c, job_id: v.job_id } : c);
    }
    // One line per skipped posting, worded exactly as the job page's gate words it, so
    // run_report counts these losses the same way (fit gate / company cap).
    for (const { c, v } of skipped) {
      // Not fit losses: already applied, or a posting the person passed on / a dead link.
      if (v.source === "applied" || v.source === "dismissed") {
        logBackend(`Skipping ${v.source === "applied" ? "duplicate" : "(passed on earlier)"}: ${c.title}`, "info");
        continue;
      }
      const why = String(v.reason || "").slice(0, 160);
      const score = v.fit_score != null ? v.fit_score : "?";
      logBackend(`⏭️ Skipped (fit ${score}): ${c.title} @ ${c.company} — ${why}`, "info");
    }
    if (skipped.length) {
      const seen = await storageGet("processedJobKeys");
      const keys = new Set(seen.processedJobKeys || []);
      for (const { c } of skipped) keys.add(c.jk);
      await storageSet({ processedJobKeys: [...keys].slice(-500) });
    }
    const fits = keep.filter((c) => c.fit).length;
    const later = keep.length - fits;
    const secs = ((Date.now() - t0) / 1000).toFixed(1);
    logBackend(
      `⚡ Judged this page ahead in ${secs} s: ${fits} fit you, ${skipped.length} don't` +
        (later ? `, ${later} checked on their page` : "") +
        (reused ? ` (${reused} remembered from earlier runs)` : "") +
        (unread ? ` · ${unread} unreadable in the pane` : ""),
      "ok"
    );
    return keep;
  }

  // A react-select keeps its typing box `<input role=combobox>` at value "" even after a
  // pick; the answer is rendered next to it as `.select__single-value` (Greenhouse
  // job-boards, captured live 10-02: "Yes" shown, input.value === ""). Read that.
  function reactSelectShownValue(el) {
    if (el.tagName !== "INPUT" || el.getAttribute("role") !== "combobox") return "";
    const box = el.closest('[class*="value-container"], [class*="ValueContainer"]');
    if (!box) return "";
    const shown = box.querySelector(
      '[class*="single-value"], [class*="singleValue"], [class*="multi-value"], [class*="multiValue"]'
    );
    return shown ? (shown.textContent || "").trim() : "";
  }

  // Greenhouse escalates a low reCAPTCHA score to a code it emails the applicant; the
  // form stays on the page and grows these boxes (night shift, live 10-01: 3/3).
  function greenhouseAsksEmailCode() {
    return !!document.querySelector(
      '#security-input-0, [id^="security-input"], input[name*="security_code"]'
    );
  }

  function collectUnfilledRequired() {
    const labels = [];
    const scope = formScope();
    // Prefer real required markers; where the platform ships none — ZipRecruiter marks
    // NOTHING required, which is why every ZR hand-back arrived with `unfilled: []` and
    // taught the ledger nothing — fall back to every visible empty field in the apply
    // modal. Those are the candidates that blocked the step, which is the whole point of
    // the ledger: it decides which deterministic handlers get built next.
    let els = scope.querySelectorAll(
      'input[required], select[required], textarea[required], [aria-required="true"]'
    );
    if (!els.length && scope !== document) {
      els = scope.querySelectorAll("input, select, textarea");
    }
    for (const el of els) {
      if (el.offsetParent === null) continue;
      const tag = el.tagName;
      if (tag !== "INPUT" && tag !== "SELECT" && tag !== "TEXTAREA") continue;
      if (el.type === "hidden" || el.type === "file") continue; // resume tracked separately
      const val = (el.value || "").trim();
      const checked = el.type === "radio" || el.type === "checkbox"
        ? !!document.querySelector(`input[name="${el.name}"]:checked`) : null;
      if (val || checked || reactSelectShownValue(el)) continue;
      const lbl = (el.labels && el.labels[0] && el.labels[0].textContent) ||
        el.getAttribute("aria-label") ||
        (el.closest("label") && el.closest("label").textContent) ||
        el.name || el.id || "(unlabeled)";
      const clean = lbl.replace(/\s+/g, " ").replace(/\*/g, "").trim().slice(0, 80).toLowerCase();
      if (clean && !labels.includes(clean)) labels.push(clean);
    }
    // Ashby marks required on the LABEL's class (_required_…), never on the control —
    // so every Ashby hand-back arrived blind (unfilled: [], questions: []) and the
    // dashboard had nothing to ask the person (Suno, live 10-08: Location + the office
    // MultiValueSelect blocked the submit, the row recorded neither). Read the label
    // marker and judge the entry's own widget: autocomplete input value, checkbox group
    // any-checked, yes/no buttons aria-pressed.
    for (const lab of scope.querySelectorAll('[class*="fieldEntry"] label[class*="required"]')) {
      const entry = lab.closest('[class*="fieldEntry"]');
      if (!entry || entry.offsetParent === null) continue;
      let answered;
      if (entry.querySelector('input[type="file"]')) continue; // resume tracked separately
      const boxes = entry.querySelectorAll('input[type="checkbox"]');
      if (entry.querySelector("[aria-pressed]")) {
        answered = !!entry.querySelector('[aria-pressed="true"]');
      } else if (boxes.length) {
        answered = !!entry.querySelector('input[type="checkbox"]:checked');
      } else {
        const ctl = entry.querySelector("input, textarea, select");
        if (!ctl) continue;
        answered = !!((ctl.value || "").trim() || reactSelectShownValue(ctl));
      }
      if (answered) continue;
      const clean = (lab.textContent || "").replace(/\s+/g, " ").replace(/\*/g, "").trim().slice(0, 80).toLowerCase();
      if (clean && !labels.includes(clean)) labels.push(clean);
    }
    return labels.slice(0, 25);
  }

  // Terminal hand-back: this job can't be completed autonomously. One message does it
  // all in background: log the reason loudly (with the link so the human can finish it),
  // merge unfilled labels into the ledger, flip the job out of `approved` (never
  // re-queues), and advance the walk. The invariant (council 2026-08-04): every approved
  // job ends submitted-complete-and-honest OR handed-back-with-a-reason — never a silent
  // half-death.
  async function handBackJob(reason, extra = {}) {
    await addHandedBackKey(extra.title, extra.company);
    await sendMsg({
      type: "ATS_JOB_FAILED",
      data: {
        reason,
        unfilled: collectUnfilledRequired(),
        // typeof guard: tests run this function alone in a vm sandbox.
        diag: typeof formBlockers === "function" ? formBlockers() : null,
        // The POSTING, not the screen we stopped on: every Indeed application runs through
        // the same smartapply step URLs, so the step URL made each new Indeed hand-back
        // overwrite the person's previous one (one open row per URL). Callers on a
        // multi-step form pass the job's own URL; an ATS form page is its posting.
        url: extra.url || window.location.href,
        title: extra.title || "", company: extra.company || "",
        platform: extra.platform || detectPlatform(),
        // How many form screens we DID complete. The user's list shows this as
        // progress, so it must be a count we actually observed — never an estimate.
        steps_done: Number(extra.steps) || 0,
      },
    });
  }

  async function goToNextPage() {
    // Build the next-page URL the same way goBackToJobList() does — never click
    // Indeed's "Next" button because it generates a URL without our search params
    // (results in q=&l=remote generic search that picks up irrelevant jobs).
    await goBackToJobList();
  }

  // =========================================================================
  // PHASE 2 — Job Detail / View Job
  // =========================================================================

  // A dead link — a stale seed row, an expired posting, a job the board pulled — renders a
  // "Not Found" page: no title, no Apply button, nothing for the walk to act on. Every
  // guard downstream keys on things such a page simply doesn't have, so the walk just sat
  // there: live 09-06, two Indeed 404s held Igor's automation windows for 31 and 15
  // minutes until the server-side stall watch noticed. Expired postings are ORDINARY, not
  // an incident — name the page for what it is and advance (skipToNextJob knows whether
  // this is a pool head, which the background then flips out of `approved`, or a native
  // walk step).
  function pageLooksNotFound() {
    const title = (document.title || "").toLowerCase();
    // Indeed serves "Not Found | Indeed"; ZipRecruiter "Page Not Found".
    if (/\bnot found\b|\b404\b/.test(title)) return true;
    const body = (document.body?.innerText || "").slice(0, 1200).toLowerCase();
    return /this job has expired|job (posting )?(you were looking for )?(was |is )?(no longer available|not found)|page (you requested |)(was |is |)not found/
      .test(body);
  }

  // Returns true when it handled a dead posting (logged + walk advanced).
  async function bailIfDeadPosting() {
    if (!pageLooksNotFound()) return false;
    const jk = (window.location.href.match(/[?&](?:vjk|jk|lk)=([a-z0-9]+)/i) || [])[1] || "";
    logBackend(`🚫 Dead link — ${platformLabel()} says this posting is gone${jk ? ` (${jk})` : ""}; moving to the next job`, "info");
    // Retire it in the pool too, or the same corpse is re-opened on every future run.
    sendMsg({ type: "REPORT_DEAD_LINK", url: window.location.href }).catch(() => {});
    await skipToNextJob();
    return true;
  }

  // "Job Title - Miami, FL 33134 - Indeed.com" -> "Job Title". Only consulted when every
  // DOM selector missed; answers "" off indeed.com so it can never invent a title elsewhere.
  function titleFromDocumentTitle() {
    const raw = (document.title || "").trim();
    if (!raw || !/indeed\.com\s*$/i.test(raw)) return "";
    const parts = raw.split(" - ").filter(Boolean);
    parts.pop(); // "Indeed.com"
    if (parts.length > 1 && /,\s*[A-Z]{2}\b|\d{5}/.test(parts[parts.length - 1])) parts.pop();
    return parts.join(" - ").trim();
  }

  // ── company on the job page ─────────────────────────────────────────────────
  // Indeed: read ONLY inside the job's own root. Every selector used to have an
  // unscoped document-wide twin, and on a results page document.querySelector(
  // '[data-testid="company-name"]') is the FIRST CARD's employer, not the open job's —
  // the wrong-employer trap #174 warned about. No root, no read: "" is honest, a
  // neighbour's name is not.
  //
  // [data-testid="vj-company-name"]: an employer without an Indeed company page has no
  // /cmp/ link in the rebuilt header; the name is a bare text node under this testid
  // (captured 10-05, tests/fixtures/indeed-viewjob-no-cmp.html). Missing it took
  // empty-company fit lines from 0% to 16% of Indeed walk postings in three weeks.
  function indeedJobRoot(doc) {
    return (
      doc.querySelector('[data-testid="viewjob-main-content"]') ||
      doc.querySelector('[data-testid="desktop-job-header"]') ||
      doc.querySelector(".jobsearch-JobComponent")
    );
  }

  function readIndeedJobCompany(doc) {
    const root = indeedJobRoot(doc);
    if (!root) return "";
    const el =
      root.querySelector('a[href*="/cmp/"]') ||
      root.querySelector('[data-testid="vj-company-name"]') ||
      root.querySelector('[data-testid="inlineHeader-companyName"]') ||
      root.querySelector('[data-testid="company-name"]') ||
      root.querySelector(".jobsearch-InlineCompanyRating-companyHeader") ||
      root.querySelector(".companyName");
    return (el?.textContent || "").replace(/\s+/g, " ").trim();
  }

  // ── location on the job page ────────────────────────────────────────────────
  // Same root, same rule as the company: the open job's place or "". The rebuilt header
  // has no testid on the city — it is the plain line right after the employer inside
  // [data-testid="company-info-metadata"] ("Adventure Loom Htx" / "Houston, TX 77074",
  // tests/fixtures/indeed-viewjob-no-cmp.html). So: the metadata's leaf lines, minus the
  // employer's own, first one shaped like a place. Then <title> ("Events Associate -
  // Houston, TX 77074 - Indeed.com"), which has outlived every rebuild — on /viewjob only,
  // where it names the open job; a results page's <title> names the search.
  const PLACE_SHAPE = /,\s*[A-Z]{2}\b|\b\d{5}\b|\bremote\b|\bhybrid\b|\bunited states\b/i;

  function readIndeedJobLocation(doc) {
    const clean = (t) => (t || "").replace(/\s+/g, " ").trim();
    const root = indeedJobRoot(doc);
    if (root) {
      const named = clean(root.querySelector('[data-testid="inlineHeader-companyLocation"]')?.textContent);
      if (named && PLACE_SHAPE.test(named)) return named;
      const meta = root.querySelector('[data-testid="company-info-metadata"]');
      if (meta) {
        const employer = clean(readIndeedJobCompany(doc));
        for (const el of meta.querySelectorAll("*")) {
          if (el.children.length) continue;
          const line = clean(el.textContent);
          if (line && line !== employer && PLACE_SHAPE.test(line)) return line;
        }
      }
    }
    if (!(doc.location?.pathname || "").startsWith("/viewjob")) return "";
    const parts = clean(doc.title).split(" - ");
    if (parts.length < 3 || !/indeed\.com$/i.test(parts[parts.length - 1])) return "";
    const fromTitle = parts[parts.length - 2];
    return PLACE_SHAPE.test(fromTitle) ? fromTitle : "";
  }

  // The posting's identity in a URL: Indeed jk/vjk, ZipRecruiter lk (the card uuid).
  function jobIdFromUrl(url) {
    try {
      const u = new URL(url, "https://www.indeed.com");
      return u.searchParams.get("jk") || u.searchParams.get("vjk") || u.searchParams.get("lk") || "";
    } catch {
      return "";
    }
  }

  // A ZipRecruiter posting has no path of its own: it is the results page plus `?lk=<uuid>`
  // (/jobs-search/2?…&lk=PpfY8jOjIWgxM4IiHjAsag, the shape in applications). Keyed by path,
  // every posting on one results page was the same "job" — the first was processed, the
  // rest were skipped as "already processed" (07-06 → 10-06; one ZR row in applications ever).
  // The uuid is also what the list phase stores per card, so both phases now agree.
  function zrDedupeKey(url) {
    return jobIdFromUrl(url) || String(url || "").split("?")[0];
  }

  // The employer the search CARD showed for this exact posting — matched by id, never by
  // position (currentJobIndex drifts on redirects/skips; a positional match would file the
  // application under the neighbour's name). Search walk → pendingJobs[].jk; pool run →
  // the server's queue row whose applyUrl carries the same id. Returns {company, source}.
  function cardCompanyFor(jobId, pendingJobs, atsPlatform, atsQueue) {
    const none = { company: "", source: "" };
    if (!jobId) return none;
    const card = (pendingJobs || []).find((j) => j && j.jk === jobId && (j.company || "").trim());
    if (card) return { company: card.company.trim(), source: "card" };
    if (atsPlatform === "pool") {
      const row = (atsQueue || []).find((q) => q && jobIdFromUrl(q.applyUrl || "") === jobId && (q.company || "").trim());
      if (row) return { company: row.company.trim(), source: "pool row" };
    }
    return none;
  }

  // The place the search card showed for this exact posting — by jk, like the company.
  function cardLocationFor(jobId, pendingJobs) {
    if (!jobId) return "";
    const card = (pendingJobs || []).find((j) => j && j.jk === jobId && (j.location || "").trim());
    return card ? card.location.trim() : "";
  }

  // ZipRecruiter right pane. Its only a[href*="/co/"] reads "Learn more about <name>"
  // plus an <svg><title>external</title> icon, so textContent produced "Learn more about
  // XPOexternal" in prod. The card's [data-testid="job-card-company"] is clean: prefer it
  // (stored card, then the card in the DOM, then the pool row — all matched by uuid), and
  // only then the link's OWN text nodes with the "Learn more about" prefix taken off.
  function readZipRecruiterCompany(doc, uuid, pendingJobs, atsPlatform, atsQueue) {
    const stored = cardCompanyFor(uuid, pendingJobs, null, null).company;
    if (stored) return stored;
    const cardEl = uuid ? doc.getElementById(`job-card-${uuid}`) : null;
    const onCard = (cardEl?.querySelector('[data-testid="job-card-company"]')?.textContent || "")
      .replace(/\s+/g, " ").trim();
    if (onCard) return onCard;
    const pool = cardCompanyFor(uuid, null, atsPlatform, atsQueue).company;
    if (pool) return pool;
    const a = doc.querySelector('[data-testid="right-pane"]')?.querySelector('a[href*="/co/"]');
    if (!a) return "";
    // aria-label carries the full name even if ZR wraps it in a <span>; the own text
    // nodes are the fallback (an <svg><title> icon is an element, so it never joins in).
    const own = Array.from(a.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent)
      .join(" ");
    for (const raw of [a.getAttribute("aria-label") || "", own]) {
      // A bare "Learn more about" must give "", or company_key collapses every ZR
      // employer into one key and the company cap blocks them all after one apply.
      const name = raw.replace(/\s+/g, " ").trim().replace(/^learn more about\b\s*/i, "").trim();
      if (name) return name;
    }
    return "";
  }

  async function phase2_jobDetail() {
    const platform = detectPlatform();
    if (await bailIfDeadPosting()) return;
    if (platform === "ziprecruiter") return await phase2_ziprecruiter();
    return await phase2_indeed();
  }

  async function phase2_indeed() {
    if (!(await isCampaignRunning())) return;

    const count = await getPlatformCount("indeed");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`Indeed daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}) — trying another platform.`, "ok");
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "indeed", reason: "platform daily cap" });
      return;
    }

    // Per-keyword slice of that cap (09-19): rotating only at page boundaries is not
    // enough, because one results page can hold more Easy Apply cards than the whole
    // daily cap — phrase #1 walked out with all 15 while "Fitter" was never searched.
    // Budget spent here → back to the list, which rotates to the next phrase.
    if (await keywordCapReached("indeed")) {
      log("This role has had its share of today's Indeed cap — switching role.", "ok");
      logBackend("Per-keyword cap reached — rotating to the next role", "info");
      await goBackToIndeedJobList();
      return;
    }

    log("On job detail page — extracting info...", "");
    await sleep(humanDelay(1500, 2500));
    if (!(await isCampaignRunning())) return; // no judge call for a stopped run

    // Extract job info.
    //
    // Indeed rebuilt the job page on React-Native-Web (live 2026-09-11): the title is an
    // h5[data-testid="vj-job-title"], the page has NO h1 at all, and the old
    // jobsearch-* classes are gone. Every selector below it missed, so the walk read
    // "no job title" on every posting and skipped the lot — Indeed applications went to
    // zero while the campaign reported itself perfectly healthy.
    //
    // The bare h1 fallback is deliberately scoped to /viewjob now. On a search page the
    // only h1 is the SERP heading ("ai engineer jobs in Miami, FL"), so the fallback
    // didn't just fail to help — it stood ready to hand a search heading to the cover
    // letter as if it were a job title.
    const onDetailPage = location.pathname.startsWith("/viewjob");
    const titleEl =
      document.querySelector('[data-testid="vj-job-title"]') ||
      document.querySelector("h1.jobsearch-JobInfoHeader-title") ||
      document.querySelector('[data-testid="jobsearch-JobInfoHeader-title"]') ||
      document.querySelector("h2.jobTitle") ||
      (onDetailPage ? document.querySelector("h1") : null);
    // Company: scoped to the job root, never document-wide (readIndeedJobCompany).
    const descEl =
      document.querySelector("#jobDescriptionText") ||
      document.querySelector(".simple-job-description-html") ||
      document.querySelector('[class*="jobDescriptionText"]') ||
      document.querySelector(".jobsearch-JobComponent-description");

    // Last resort when every selector missed: Indeed's <title> ("Job Title - City, ST
    // 12345 - Indeed.com") has outlived every rebuild of this page, including the 2026-09
    // react-native-web one. The next redesign should cost us a slightly worse title, not
    // the walk — 84 postings were skipped on "no job title" before this line existed.
    const jobTitle = titleEl?.textContent?.trim() || titleFromDocumentTitle();
    let jobCompany = readIndeedJobCompany(document);
    let jobLocation = readIndeedJobLocation(document);
    // 3000, matching the ATS path. 1000 was set when this text only fed a prompt the
    // server clipped anyway; it is now STORED (POST /jobs/describe) and read by three
    // consumers that clip at their own limits — fit judge 2500, resume tailor 1500,
    // cover letter 500. At 1000 the tailor was starved of a third of its window for
    // free: a bigger slice costs one HTTP payload, not one token.
    const jobDesc = readJobDescription(descEl).slice(0, 3000);
    const jobUrl = window.location.href;

    if (!jobTitle) {
      // Durable, not popup-only: a page with no title is exactly the shape that used to
      // end the walk in silence — nothing in the activity log to tell it from a freeze.
      log("Could not find job title — skipping", "err");
      logBackend(`⏭️ No job title on this page (${location.pathname}) — skipping to the next job`, "warn");
      // One unreadable page is a bad posting. A STREAK of them is a broken platform —
      // the 2026-09-11 DOM rebuild produced 84 of these in a row while the walk kept
      // grinding Indeed and the user watched a "running" campaign apply to nothing,
      // concluding the product was broken. After UNREADABLE_STREAK_LIMIT in a row, say
      // so out loud and hand the walk to the next platform (the PLATFORM_EXHAUSTED
      // failover already knows how); Indeed gets retried automatically on the next run.
      const u = await storageGet("unreadableStreak");
      const streak = (u.unreadableStreak || 0) + 1;
      await storageSet({ unreadableStreak: streak });
      if (streak >= UNREADABLE_STREAK_LIMIT) {
        await storageSet({ unreadableStreak: 0 });
        logBackend(
          `⚠️ ${streak} job pages in a row were unreadable — Indeed likely changed its layout. ` +
          "Moving on to another platform so your run keeps producing; we'll fix Indeed on our side.",
          "warn"
        );
        await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "indeed", reason: "job pages unreadable — layout change suspected" });
        return;
      }
      await skipToNextJob();
      return;
    }
    // A readable page breaks the streak: scattered bad postings must never add up to
    // a false "platform broken" verdict over a long healthy run.
    await storageSet({ unreadableStreak: 0 });

    // The page had no employer we could read — but the card we opened it from did
    // ("Opening job: X @ Y" then "Good fit: X @ " in prod). Take it back ONLY for the
    // same jk; the line lets prod count how often the page alone falls short.
    if (!jobCompany || !jobLocation) {
      const st = await storageGet(["pendingJobs", "atsPlatform", "atsQueue"]);
      if (!jobCompany) {
        const fb = cardCompanyFor(jobIdFromUrl(jobUrl), st.pendingJobs, st.atsPlatform, st.atsQueue);
        if (fb.company) {
          jobCompany = fb.company;
          logBackend(`🏷️ company from ${fb.source}: ${fb.company}`, "info");
        }
      }
      if (!jobLocation) jobLocation = cardLocationFor(jobIdFromUrl(jobUrl), st.pendingJobs);
    }

    // Deduplicate by job key (jk= / vjk= in URL).
    // Indeed jk values are alphanumeric, NOT just hex — the original [a-f0-9]+
    // regex silently failed on keys containing g-z, leaving jobKey=null and
    // causing the same job to be re-processed on every content.js reload.
    const jkMatch = jobUrl.match(/[?&](?:vjk|jk)=([a-z0-9]+)/i);
    const jobKey = jkMatch ? jkMatch[1] : null;
    // Fallback: deduplicate by URL if no jk present
    const dedupeKey = jobKey || jobUrl.split("?")[0];
    {
      const seen = await storageGet("processedJobKeys");
      const keys = seen.processedJobKeys || [];
      if (keys.includes(dedupeKey)) {
        log(`${jobTitle} — already processed, skipping`, "");
        logBackend(`Skipping duplicate: ${jobTitle}`, "info");
        await skipToNextJob();
        return;
      }
      await storageSet({ processedJobKeys: [...keys, dedupeKey].slice(-500) });
    }

    log(`Job: ${jobTitle} @ ${jobCompany}`, "");

    // Keyword relevance check — skip jobs whose title shares NO words with any
    // campaign keyword. Loosened from strict AND-matching (every word of a phrase
    // had to appear) to OR-matching (at least one keyword word in the title):
    // strict matching skipped clearly-relevant roles, e.g. "Senior Marketing
    // Manager" failed both "healthcare marketing" and "social media manager"
    // because no single phrase matched in full. Still blocks fully off-target
    // titles (e.g. "Provider Relations Specialist") from wasting cover-letter calls.
    // The list phase already ran this on the card title; this is the backstop for a card
    // whose title was truncated. Pool swipe runs get no keywords (titleGateKeywords).
    if (!titleMatchesKeywords(jobTitle, await titleGateKeywords())) {
      log(`${jobTitle} — title doesn't match keywords, skipping`, "");
      logBackend(`Skip (title mismatch): ${jobTitle} @ ${jobCompany}`, "info");
      await skipToNextJob();
      return;
    }

    // Fit Engine M1 — decide whether to apply at ALL before spending a cover
    // letter + application on a wrong-fit job. Runs after the cheap keyword filter
    // and before the expensive steps. Skips roles the resume clearly can't support
    // (too senior, missing hard requirements) with an honest, logged reason. Fails
    // CLOSED (ROADMAP_E2E.md P1): a judge error/timeout/401 now SKIPS the job rather
    // than applying blindly — never spray applications under the user's identity when
    // we couldn't verify fit. Also improves throughput — no grinding bad-fit forms.
    {
      // POOL SWIPE RUN: the user already approved this job by swiping — the AI fit
      // gate must never re-veto their explicit pick. Legacy TAP reviewMode also skips
      // the gate (you are the filter). AUTO mode runs it and FAILS CLOSED (a judge
      // error/timeout/401 skips — never spray applications under the user's identity
      // when we couldn't verify fit).
      const _poolRun = (await storageGet("atsPlatform")).atsPlatform === "pool";
      const reviewMode = (await storageGet("reviewMode")).reviewMode === true;
      if (_poolRun) {
        logBackend(`Applying your approved pick: ${jobTitle} @ ${jobCompany}`, "info");
      } else if (reviewMode) {
        logBackend(`${jobTitle} @ ${jobCompany} — ready for your tap`, "info");
      } else {
        // Judged on the results page already: the server answers from the stored verdict
        // (and still checks the company cap first), no second judge.
        const pending = (await storageGet("pendingJobs")).pendingJobs || [];
        const prejudgedId = (jobKey && pending.find((j) => j.jk === jobKey)?.job_id) || null;
        const fit = await sendMsg({
          type: "ASSESS_FIT",
          data: {
            job_title: jobTitle,
            company: jobCompany,
            description: jobDesc,
            ...(prejudgedId ? { job_id: prejudgedId } : {}),
          },
        });
        // The judge takes seconds. Live 10-08: stopped 05:43:15, then "✓ Good fit (42)" at
        // :23 — the answer to a call made before Stop, acted on after it.
        if (!(await isCampaignRunning())) return;
        if (!fit || fit.decision !== "apply") {
          const why = (fit && fit.reason ? fit.reason : "fit check unavailable — skipped for safety").slice(0, 160);
          log(`Skipping ${jobTitle} — ${why}`, "");
          logBackend(`⏭️ Skipped (fit ${(fit && fit.fit_score != null) ? fit.fit_score : "?"}): ${jobTitle} @ ${jobCompany} — ${why}`, (!fit || fit.failClosed) ? "warn" : "info");
          await skipToNextJob();
          return;
        }
        if (fit.judged) {
          logBackend(`✓ Good fit (${fit.fit_score}): ${jobTitle} @ ${jobCompany}`, "info");
        }
      }
    }

    // Save current job context
    await storageSet({
      currentJobInfo: { title: jobTitle, company: jobCompany, description: jobDesc, url: jobUrl },
    });
    await recordJobDescription(jobTitle, jobCompany, jobDesc, jobUrl, jobLocation);

    // The cover letter is written later, and only if the apply form actually asks for
    // one (ensureCoverLetter, called from the form filler). Indeed's wizard has never
    // shown a cover-letter step in 214 measured form loads, so generating here charged
    // us for a letter that had nowhere to go — and then History displayed it as sent.

    // Find and click the Apply button — poll up to 8 s for async panel load
    await sleep(humanDelay(1000, 2000));

    const applyBtn = await waitForApplyButton(8000);
    // waitForApplyButton answers null on Stop too — that is not "no Apply button" (live 10-08:
    // "Skip (no Apply button)" two seconds after a Stop, then a skip to the next job).
    if (!(await isCampaignRunning())) return;
    if (!applyBtn) {
      // After 8s of polling, decide why: external-only or genuinely no button
      const isExternal = !!document.querySelector('button[aria-label*="company site" i], a[aria-label*="company site" i]');
      if (isExternal) {
        log(`${jobTitle} — external apply only, skipping`, "");
        logBackend(`Skip (external apply): ${jobTitle} @ ${jobCompany}`, "info");
      } else {
        log("No Apply button found — skipping", "err");
        logBackend(`Skip (no Apply button): ${jobTitle} @ ${jobCompany}`, "error");
      }
      await skipToNextJob();
      return;
    }

    log("Clicking Apply button...", "");
    logBackend(`Clicking Apply: ${jobTitle} @ ${jobCompany}`, "info");
    if (shouldMisclick()) await performMisclick(applyBtn);
    await humanClick(applyBtn);

    // Watchdog: the Indeed apply form must show up within ~18s. If it doesn't, this
    // "Apply" routed to an external ATS ("Apply with Indeed" that redirects to the
    // employer's system, e.g. Precision AQ), opened a new tab, or did nothing —
    // phase3 is only driven by the MutationObserver seeing the form, so without this
    // the campaign HANGS on the job forever. The job is already in processedJobKeys
    // (marked above before applying), so skipping here won't re-loop onto it.
    const formShowed = await waitForFormVisible(18000);
    if (!(await isCampaignRunning())) return;
    if (!formShowed) {
      // The click may have opened a tab: the wizard working on its own (step aside — it
      // owns the walk now), or a page that is not an application (close it, walk on HERE).
      if (await reclaimAfterAbandonedApply(jobTitle, jobCompany)) return;
      log(`${jobTitle} — no Indeed form after Apply (external/unsupported), skipping`, "");
      logBackend(`Skip (no form after Apply): ${jobTitle} @ ${jobCompany}`, "info");
      await skipToNextJob();
      return;
    }
    // Form appeared. For an IN-PAGE Easy Apply modal (no navigation) we must drive
    // phase3 DIRECTLY here: this phase2 is still on the stack inside runPhase(), so
    // the MutationObserver's runPhase() calls are suppressed by the _runPhaseActive
    // guard — relying on the observer would drop the phase3 trigger and hang the job
    // with an empty form. Setting lastPhase avoids a duplicate observer-driven run.
    // (The navigation-to-smartapply case doesn't reach here — that page unloads this
    // content script and a fresh one drives phase3 via init().)
    if (isFormVisible()) {
      lastPhase = "form";
      await phase3_fillForm();
    }
  }

  // ---- Abandoned apply: who owns the walk now? ----
  // An Apply click that produced no form in THIS tab may still have opened one. Background
  // (reclaimCampaignTab) decides: if the campaign moved to a tab of its own (Indeed's wizard
  // in a new tab, adopted by the capture tick), answer true — the caller must step aside,
  // because that tab is filling the form and will walk on from there. Otherwise it closes the
  // tabs this one opened, brings this one to the front, and the caller skips as before.
  // Live 10-06 (ext 1.8.43): the stray www.indeed.com/job/… tab took the campaign, this tab
  // skipped onto the next /viewjob as "not the campaign tab", and the run sat dead for 3 min.
  async function reclaimAfterAbandonedApply(jobTitle, jobCompany) {
    let r = null;
    try { r = await sendMsg({ type: "RECLAIM_CAMPAIGN_TAB" }); } catch { r = null; }
    if (r && r.moved) {
      logBackend(`↪️ Apply for ${jobTitle} @ ${jobCompany} went on in its own tab (${r.url || "?"}) — the walk continues there`, "info");
      return true;
    }
    if (r && Array.isArray(r.closed) && r.closed.length) {
      logBackend(`🧹 Closed ${r.closed.length} tab(s) the Apply click left open (${r.closed.join(", ")}) — the walk stays here`, "info");
    }
    return false;
  }

  async function waitForFormVisible(timeoutMs = 18000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (!(await isCampaignRunning())) return false;
      // The apply flow often navigates to smartapply.indeed.com — that counts as
      // "form is coming" even before ia-* nodes render.
      if (window.location.href.includes("smartapply.indeed.com")) return true;
      if (isFormVisible()) return true;
      await sleep(500);
    }
    return false;
  }

  function findApplyButton() {
    const selectors = SELECTORS.applyButton || FALLBACK_SELECTORS.applyButton;

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }

    // Text fallback — match "Apply now", "Apply with Indeed", "Easily apply"
    // but never "Apply on company site" (external links, can't automate)
    const buttons = document.querySelectorAll("button, a");
    for (const btn of buttons) {
      const text = btn.textContent?.trim() || "";
      const label = btn.getAttribute("aria-label") || "";
      const combined = `${text} ${label}`.toLowerCase();
      if (/company\s*site/i.test(combined)) continue;
      if (/^(apply now|apply with indeed|easily apply|apply)$/i.test(text) && btn.offsetParent !== null) {
        return btn;
      }
    }
    return null;
  }

  async function waitForApplyButton(timeoutMs = 8000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (!(await isCampaignRunning())) return null;
      const btn = findApplyButton();
      if (btn) return btn;
      await sleep(500);
    }
    return null;
  }

  // =========================================================================
  // ZIPRECRUITER — Phase 1 (job list) + Phase 2 (job detail)
  // =========================================================================

  async function phase1_ziprecruiter() {
    if (!(await isCampaignRunning())) return;

    const count = await getPlatformCount("ziprecruiter");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`ZipRecruiter daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}) — trying another platform.`, "ok");
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "ziprecruiter", reason: "platform daily cap" });
      return;
    }

    log("Scanning ZipRecruiter for Quick Apply jobs...", "");
    logBackend("Scanning ZipRecruiter job list…", "info");
    await sleep(humanDelay(2000, 3000));

    const alreadyApplied = await getAppliedUrls();
    const seenKeys = await storageGet("processedJobKeys");
    const processedKeys = new Set(seenKeys.processedJobKeys || []);

    // Confirmed real selector: .job_result_two_pane_v2 wraps each job card
    const wrappers = Array.from(document.querySelectorAll(".job_result_two_pane_v2"));
    if (!wrappers.length) {
      // Results ran out for THIS phrase at this depth. Paging on regardless burns ~25s
      // per empty page until the budget is spent — live 08-21 the engine walked a 5-page
      // result set to page 18 before giving up. Deeper laps of the same phrase can only
      // be emptier, so retire it for this run and let the list nav rotate to the next.
      // (The old guard waited for two empty pages IN A ROW. Under the breadth walk those
      // two would be two DIFFERENT phrases, so it would retire the wrong one.)
      log("No job cards found on ZipRecruiter — trying the next role", "");
      logBackend("No ZR results for this role — retiring it for this run", "warn");
      await retireKeyword();
      // goBackToJobList rotates, or emits PLATFORM_EXHAUSTED when nothing is left.
      await goBackToJobList();
      return;
    }

    // Build base search URL (without lk=) for constructing per-job URLs
    const baseUrl = new URL(window.location.href);
    baseUrl.searchParams.delete("lk");
    const baseSearch = baseUrl.toString();

    const candidates = [];
    for (const wrapper of wrappers) {
      // Quick Apply badge. ZR moved the text around: the FIRST .text-brand in a card is
      // now an EMPTY node, so first-match + text-test silently rejected real Quick Apply
      // cards (live-diagnosed 2026-08-15). Scan ALL badge candidates for the text instead.
      // ZR ships TWO badge wordings side by side on the same results page (live 08-15:
      // "Quick apply" AND "1-click apply"). Matching only the first made HALF the native
      // inventory invisible — which is most of the "ZR has no jobs for us" story.
      const badgeNodes = wrapper.querySelectorAll("div[class*='bg-badge-brand'] p, .text-brand, p[class*='text-brand']");
      const isQuickApply = Array.from(badgeNodes).some((el) => ZR_NATIVE_BADGE_RE.test(el.textContent || ""));
      if (!isQuickApply) continue;

      const article = wrapper.querySelector("article");
      if (!article) continue;

      // UUID is in article id: "job-card-{UUID}"
      const uuid = article.id?.replace("job-card-", "") || "";
      if (!uuid) continue;

      // Title is in button[aria-label^="View "] > h2
      const titleBtn = article.querySelector('button[aria-label^="View "]');
      const title = titleBtn?.querySelector("h2")?.textContent?.trim() || "";
      if (!title) continue;

      const company = article.querySelector('[data-testid="job-card-company"]')?.textContent?.trim() || "";

      // Job "URL" = list page + lk param — triggers detail phase on full reload
      const jobUrl = baseSearch + (baseSearch.includes("?") ? "&" : "?") + "lk=" + uuid;

      if (alreadyApplied.has(jobUrl)) continue;
      if (processedKeys.has(uuid)) continue;

      // Same reasoning as the Indeed snippet above: the card's own blurb is the only
      // description that exists before the posting is opened, and it costs no extra
      // page load. ZR does not label it, so take the card's longest paragraph — the
      // short ones are salary/location chips.
      const zrParas = Array.from(article.querySelectorAll("p"))
        .map((el) => (el.textContent || "").replace(/\s+/g, " ").trim())
        .filter((t) => t.length > 60);
      const snippet = (zrParas.sort((a, b) => b.length - a.length)[0] || "").slice(0, 1500);

      candidates.push({ title, company, url: jobUrl, jk: uuid, snippet });
    }

    // HARVEST-TO-POOL (P0c 2026-07-29): server-side ZR scraping is dead (JobSpy → CF 403),
    // so — exactly like Indeed — every Quick Apply card this browser SEES goes to the pool.
    // The link is the search-URL + lk=<uuid> form: that IS ZR's single-job page (detectPhase
    // → "detail" → right-pane apply), so the by-link pool executor can walk it with the
    // selectors we already have. Fire-and-forget; server dedups known links.
    // BEFORE the title gate, as on Indeed: the pool is a shared crawl index.
    try {
      const zrHarvest = candidates
        .map((j) => ({
          title: j.title || "",
          company: j.company || "",
          link: j.url || "",
          platform: "ziprecruiter",
          description: j.snippet || "",
        }))
        .filter((j) => j.link && j.title);
      if (zrHarvest.length) {
        Promise.resolve(sendMsg({ type: "INGEST_JOBS", data: { jobs: zrHarvest } })).catch(() => {});
      }
    } catch (_) { /* harvest is best-effort */ }

    // Title gate on the card (same rule as the detail phase), before anything is opened.
    const { keep: quickApplyJobs, skipped: offTitle } =
      splitCardsByTitle(candidates, await titleGateKeywords());
    if (offTitle.length) logBackend(titleSkipSummary(offTitle, candidates.length), "info");

    // All filtered = an empty page: next page/phrase, and the phrase is NOT retired (only
    // a search with no cards at all retires it, above).
    if (!quickApplyJobs.length) {
      log("No new Quick Apply jobs found — checking next page...", "");
      logBackend(candidates.length
        ? "None of this page's Quick Apply jobs match your roles — going to next"
        : "No Quick Apply jobs on this ZipRecruiter page", "info");
      await goBackToJobList();
      return;
    }

    log(`Found ${quickApplyJobs.length} Quick Apply jobs`, "ok");
    logBackend(`Found ${quickApplyJobs.length} Quick Apply jobs on ZipRecruiter`, "ok");

    await storageSet({
      pendingJobs: quickApplyJobs,
      currentJobIndex: 0,
    });

    const first = quickApplyJobs[0];
    log(`Opening: ${first.title} @ ${first.company}`, "");
    logBackend(`Opening ZipRecruiter job: ${first.title} @ ${first.company}`, "info");
    await sleep(humanDelay(2000, 4000));
    if (!(await isCampaignRunning())) return;
    window.location.href = first.url;
  }

  async function phase2_ziprecruiter() {
    if (!(await isCampaignRunning())) return;

    const count = await getPlatformCount("ziprecruiter");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`ZipRecruiter daily limit reached — trying another platform.`, "ok");
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "ziprecruiter", reason: "platform daily cap" });
      return;
    }

    // Per-keyword slice of that cap (09-19): rotating only at page boundaries is not
    // enough, because one results page can hold more Easy Apply cards than the whole
    // daily cap — phrase #1 walked out with all 15 while "Fitter" was never searched.
    // Budget spent here → back to the list, which rotates to the next phrase.
    if (await keywordCapReached("ziprecruiter")) {
      log("This role has had its share of today's ZipRecruiter cap — switching role.", "ok");
      logBackend("Per-keyword cap reached — rotating to the next role", "info");
      await goBackToZipRecruiterJobList();
      return;
    }

    log("ZipRecruiter job detail — waiting for right panel...", "");
    // Right panel loads asynchronously after URL pushState update
    const panelReady = await waitForZipRecruiterRightPanel(8000);
    if (!panelReady) {
      log("ZipRecruiter right panel never loaded — skipping", "err");
      await skipToNextJob();
      return;
    }
    await sleep(humanDelay(500, 1000));

    const panel = document.querySelector('[data-testid="right-pane"]');
    const titleEl = panel?.querySelector("h2");
    const descEl = document.querySelector('[data-testid="job-details-scroll-container"]');

    const jobTitle = titleEl?.textContent?.trim() || "";
    const _zrSt = await storageGet(["pendingJobs", "atsPlatform", "atsQueue"]);
    const jobCompany = readZipRecruiterCompany(
      document, jobIdFromUrl(window.location.href), _zrSt.pendingJobs, _zrSt.atsPlatform, _zrSt.atsQueue
    );
    const jobDesc = descEl?.textContent?.trim().slice(0, 3000) || "";  // see the note on the /viewjob path
    const jobUrl = window.location.href;

    if (!jobTitle) {
      log("Could not find job title on ZipRecruiter — skipping", "err");
      await skipToNextJob();
      return;
    }

    // Dedup — session (processedJobKeys) AND cross-session (appliedUrls). Without
    // the appliedUrls check a job applied in a PREVIOUS campaign got re-applied on
    // the next run — a real duplicate to the employer (seen live: Sushi House twice).
    const dedupeKey = zrDedupeKey(jobUrl);
    {
      const appliedSet = await getAppliedUrls();
      const appliedJobs = await getAppliedJobKeys();
      if (appliedSet.has(dedupeKey) || appliedSet.has(jobUrl) || appliedJobs.has(jobDedupKey(jobTitle, jobCompany))) {
        log(`${jobTitle} — already applied in a previous run, skipping`, "");
        logBackend(`Skip (already applied): ${jobTitle} @ ${jobCompany}`, "info");
        await skipToNextJob();
        return;
      }
      if ((await getHandedBackKeys()).has(jobDedupKey(jobTitle, jobCompany))) {
        logBackend(`Skip (handed back earlier today): ${jobTitle} @ ${jobCompany}`, "info");
        await skipToNextJob();
        return;
      }
      const seen = await storageGet("processedJobKeys");
      const keys = seen.processedJobKeys || [];
      if (keys.includes(dedupeKey)) {
        log(`${jobTitle} — already processed, skipping`, "");
        logBackend(`Skip (already processed this run): ${jobTitle} @ ${jobCompany}`, "info");
        await skipToNextJob();
        return;
      }
      await storageSet({ processedJobKeys: [...keys, dedupeKey].slice(-500) });
    }

    log(`Job: ${jobTitle} @ ${jobCompany}`, "");

    // Keyword relevance check — the backstop behind the list-phase card filter (same
    // rule, same keywords, same pool exception: titleGateKeywords).
    if (!titleMatchesKeywords(jobTitle, await titleGateKeywords())) {
      log(`${jobTitle} — title doesn't match keywords, skipping`, "");
      logBackend(`Skip (title mismatch): ${jobTitle} @ ${jobCompany}`, "info");
      await skipToNextJob();
      return;
    }

    // Already applied on the platform itself (a previous run submitted it, or the user
    // did). ZipRecruiter flips the pane button to "Applied". Catch it BEFORE the fit
    // judge and the cover letter — those cost money and the answer can only be "skip".
    // (Live 08-15 this showed up as a misleading "Skip (no Quick Apply button)".)
    if (jobLooksApplied()) {
      logBackend(`Already applied on ZipRecruiter — skipping: ${jobTitle} @ ${jobCompany}`, "info");
      await addAppliedJobKey(jobTitle, jobCompany);
      await skipToNextJob();
      return;
    }

    // Native-apply gate FIRST — before the fit judge and the cover letter. Both cost
    // money and neither can rescue an external-apply posting: the only possible outcome
    // is "skip". ZR's card badge ("1-click apply" / "Quick apply") is NOT a promise —
    // live 08-15 several badged cards opened panes with no apply button at all.
    await sleep(humanDelay(800, 1500));
    const applyBtn = await waitForZipRecruiterApplyButton(8000);
    if (!(await isCampaignRunning())) return; // null on Stop is not "no Quick Apply button"
    if (!applyBtn) {
      // Diagnose WHY: is this a genuine external-apply job, or a selector miss?
      // Report the panel's actual buttons/apply-links so we learn from our own logs.
      try {
        const panel = document.querySelector('[data-testid="right-pane"]') || document.body;
        const vis = (el) => el && el.offsetParent !== null;
        const btns = Array.from(panel.querySelectorAll("button")).filter(vis)
          .map((b) => (b.textContent || b.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim())
          .filter(Boolean).slice(0, 10).join(" | ");
        const applyLinks = Array.from(panel.querySelectorAll("a")).filter(vis)
          .map((a) => (a.textContent || "").replace(/\s+/g, " ").trim())
          .filter((t) => /apply/i.test(t)).slice(0, 4).join(" | ");
        const line = `APPLY DIAG [${jobTitle.slice(0, 30)}] btns=[${btns}] applyLinks=[${applyLinks}]`;
        log(line, "");
        logBackend(line, "info"); // durable on backend — survives osascript channel loss
      } catch (e) { log(`APPLY DIAG error: ${e.message}`, ""); }
      // P4: external-apply job → try to route to its ATS (Greenhouse/Lever) instead of
      // skipping. Navigates away if a supported ATS URL is found + wiring is enabled.
      if (await routeExternalToAts(jobTitle, jobCompany, document.querySelector('[data-testid="right-pane"]') || document.body)) return;
      log(`${jobTitle} — no Quick Apply button found, skipping`, "");
      logBackend(`Skip (no Quick Apply button): ${jobTitle} @ ${jobCompany}`, "info");
      // External-apply wall guard (Igor 2026-08-15: the campaign must not grind a board
      // that has no native supply — switch boards autonomously). N externals in a row →
      // hand the decision to the background, which fails over or stops.
      const exd = await storageGet("zrNoBtnStreak");
      const streak = (exd.zrNoBtnStreak || 0) + 1;
      await storageSet({ zrNoBtnStreak: streak });
      if (streak >= 6) {
        logBackend(`ZipRecruiter: ${streak} external-apply jobs in a row — switching platform`, "warn");
        await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "ziprecruiter", reason: "external-apply streak" });
        return;
      }
      await skipToNextJob();
      return;
    }

    await storageSet({ zrNoBtnStreak: 0 }); // native supply confirmed — reset the wall guard

    // Fit Engine M1
    {
      // POOL SWIPE RUN: the user already approved this job by swiping — the AI fit
      // gate must never re-veto their explicit pick. Legacy TAP reviewMode also skips
      // the gate (you are the filter). AUTO mode runs it and FAILS CLOSED (a judge
      // error/timeout/401 skips — never spray applications under the user's identity
      // when we couldn't verify fit).
      const _poolRun = (await storageGet("atsPlatform")).atsPlatform === "pool";
      const reviewMode = (await storageGet("reviewMode")).reviewMode === true;
      if (_poolRun) {
        logBackend(`Applying your approved pick: ${jobTitle} @ ${jobCompany}`, "info");
      } else if (reviewMode) {
        logBackend(`${jobTitle} @ ${jobCompany} — ready for your tap`, "info");
      } else {
        const fit = await sendMsg({
          type: "ASSESS_FIT",
          data: { job_title: jobTitle, company: jobCompany, description: jobDesc },
        });
        if (!(await isCampaignRunning())) return; // Stop landed while the judge answered
        if (!fit || fit.decision !== "apply") {
          const why = (fit && fit.reason ? fit.reason : "fit check unavailable — skipped for safety").slice(0, 160);
          log(`Skipping ${jobTitle} — ${why}`, "");
          logBackend(`⏭️ Skipped (fit ${(fit && fit.fit_score != null) ? fit.fit_score : "?"}): ${jobTitle} @ ${jobCompany} — ${why}`, (!fit || fit.failClosed) ? "warn" : "info");
          await skipToNextJob();
          return;
        }
        if (fit.judged) {
          logBackend(`✓ Good fit (${fit.fit_score}): ${jobTitle} @ ${jobCompany}`, "info");
        }
      }
    }

    await storageSet({
      currentJobInfo: { title: jobTitle, company: jobCompany, description: jobDesc, url: jobUrl },
    });
    await recordJobDescription(jobTitle, jobCompany, jobDesc, jobUrl);

    // No cover letter here either — see ensureCoverLetter: it is written by the form
    // filler, and only when the form shows a field for it.

    // Re-find the button: the pane can re-render while the fit judge and the cover
    // letter are being generated, which detaches the node we matched earlier.
    const applyBtn2 = findZipRecruiterApplyButton() || (await waitForZipRecruiterApplyButton(8000));
    if (!(await isCampaignRunning())) return; // never click Quick Apply for a stopped run
    if (!applyBtn2) {
      logBackend(`Skip (apply button vanished mid-flow): ${jobTitle} @ ${jobCompany}`, "warn");
      await skipToNextJob();
      return;
    }

    await storageSet({ zrNoBtnStreak: 0 }); // native supply confirmed — reset the wall guard
    log("Clicking Quick Apply...", "");
    logBackend(`Clicking Quick Apply: ${jobTitle} @ ${jobCompany}`, "info");
    await humanClick(applyBtn2);

    // Wait for the apply modal or form to appear
    // 40s, not 15: the apply modal renders lazily and the automation window is
    // background-throttled (measured ~30s on 08-21). A short wait doesn't fail fast, it
    // just abandons applications that were about to become fillable.
    const formReady = await waitForZipRecruiterForm(40000);
    if (!(await isCampaignRunning())) return;
    if (!formReady) {
      if (await reclaimAfterAbandonedApply(jobTitle, jobCompany)) return;
      log(`${jobTitle} — no Quick Apply form appeared (external ATS), skipping`, "");
      logBackend(`Skip (no ZR form after 40s): ${jobTitle} @ ${jobCompany} — ${dialogSnapshot()}`, "info");
      await skipToNextJob();
      return;
    }

    // Drive phase3 directly — same reason as Indeed: phase2 is on stack,
    // MutationObserver's runPhase() is suppressed by _runPhaseActive guard.
    lastPhase = "form";
    await phase3_fillForm();
  }

  // A job whose application was already STARTED (a previous run filled a step and never
  // finished) shows "Continue" instead of "Quick Apply" — live 08-15:
  // APPLY DIAG btns=[Continue | Share this job | Report] applyLinks=[]. Treating that as
  // "external apply, skip" permanently orphaned every job a broken run had touched.
  const ZR_NATIVE_BADGE_RE = /quick\s*apply|1[\s-]?click\s*apply/i;
  const ZR_APPLY_RE = /^(quick apply|1[\s-]?click apply|continue( application)?)$/;
  function isZipRecruiterApplyBtn(el) {
    if (!el || el.offsetParent === null) return false;
    const label = ((el.getAttribute("aria-label") || "") || (el.textContent || "")).replace(/\s+/g, " ").trim().toLowerCase();
    const text = (el.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
    return ZR_APPLY_RE.test(label) || ZR_APPLY_RE.test(text);
  }

  function findZipRecruiterApplyButton() {
    // Confirmed real selector: button[aria-label="Quick Apply"] inside [data-testid="right-pane"]
    const panel = document.querySelector('[data-testid="right-pane"]');
    if (panel) {
      const btn = panel.querySelector('button[aria-label="Quick Apply"]');
      if (btn && btn.offsetParent !== null) return btn;
      for (const el of panel.querySelectorAll("button")) {
        if (isZipRecruiterApplyBtn(el)) return el;
      }
    }
    // Fallback: any visible Quick-Apply/Continue button anywhere on the page
    for (const el of document.querySelectorAll('button')) {
      if (isZipRecruiterApplyBtn(el)) return el;
    }
    return null;
  }

  async function waitForZipRecruiterRightPanel(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (!(await isCampaignRunning())) return false;
      const panel = document.querySelector('[data-testid="right-pane"]');
      // Panel is ready when it has an h2 (job title loaded)
      if (panel && panel.querySelector("h2")) return true;
      await sleep(400);
    }
    return false;
  }

  // =========================================================================
  // P4 — board external-apply → ATS wiring (ROADMAP_E2E.md P4)
  // Most good-fit board jobs are "external apply" that funnel to Greenhouse/Lever.
  // Instead of skipping them, route the campaign tab to the ATS URL so phase_ats
  // (already fail-closed: fit-gate + resume-guard + review-mode) applies, then return
  // to the board to continue. Gated behind the `atsWiring` flag (default OFF) until
  // validated on an observed live run — untested navigation must not reach prod on.
  // =========================================================================
  const ATS_URL_RE = /(?:job-boards|boards)\.greenhouse\.io|jobs\.lever\.co/i;

  function findExternalAtsUrl(scope) {
    scope = scope || document;
    for (const a of scope.querySelectorAll("a[href]")) {
      const href = a.href || "";
      if (ATS_URL_RE.test(href)) return href;
      // Boards often wrap the real destination in a redirect query param.
      const m = href.match(/[?&](?:url|redirect|redirect_url|to|dest|apply_url)=([^&]+)/i);
      if (m) { try { const dec = decodeURIComponent(m[1]); if (ATS_URL_RE.test(dec)) return dec; } catch { /* not a URL */ } }
    }
    for (const el of scope.querySelectorAll("[data-href],[data-url],[data-apply-url]")) {
      const cand = el.getAttribute("data-href") || el.getAttribute("data-url") || el.getAttribute("data-apply-url") || "";
      if (ATS_URL_RE.test(cand)) return cand;
    }
    return null;
  }

  async function atsWiringEnabled() {
    return (await storageGet("atsWiring")).atsWiring === true;
  }

  // Returns true if it navigated to an ATS (caller must NOT then skipToNextJob).
  async function routeExternalToAts(jobTitle, jobCompany, scope) {
    if (!(await atsWiringEnabled())) return false;
    const url = findExternalAtsUrl(scope);
    if (!url) return false;
    // Mark applied FIRST so returning to the board list can't re-open this same job.
    await addAppliedJobKey(jobTitle, jobCompany);
    await storageSet({ atsReturnUrl: window.location.href });
    logBackend(`↗️ External→ATS: ${jobTitle} @ ${jobCompany} — routing to ${url.slice(0, 90)}`, "info");
    await sleep(humanDelay(800, 1500));
    if (!(await isCampaignRunning())) return true; // stopped: the caller must not act either
    window.location.href = url;
    return true;
  }

  // Called after phase_ats finishes (any exit) — if we arrived from a board, go back so
  // the campaign continues. No-op for standalone ATS tabs (atsReturnUrl unset).
  async function returnToBoardAfterAts() {
    const { atsReturnUrl } = await storageGet("atsReturnUrl");
    if (!atsReturnUrl) return;
    await storageRemove("atsReturnUrl");
    if (!(await isCampaignRunning())) return;
    logBackend("↩️ Returning to board search after ATS apply", "info");
    await sleep(humanDelay(1500, 3000));
    if (!(await isCampaignRunning())) return;
    window.location.href = atsReturnUrl;
  }

  async function waitForZipRecruiterApplyButton(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (!(await isCampaignRunning())) return null;
      const btn = findZipRecruiterApplyButton();
      if (btn) return btn;
      await sleep(500);
    }
    return null;
  }

  // A ZipRecruiter dialog is a REAL Quick Apply form only if it has form fields OR a
  // genuine apply/submit action button. Many ZR "Quick Apply" jobs are actually
  // external-apply: clicking Quick Apply opens a dialog with NO fields and only a
  // "Close" button (verified live: btns=[Close | Close], inputs=0). The old heuristic
  // ("Quick Apply" heading text) misfired on these, wasting a full phase3 cycle per
  // job. Requiring a field or a real action button skips external jobs fast.
  function isZipRecruiterApplyForm(d) {
    if (!d || !d.offsetParent) return false;
    if (d.querySelector('input[type="text"], input[type="tel"], input[type="email"], textarea, select, input[type="file"], input[name], [role="combobox"]')) {
      return true;
    }
    for (const b of d.querySelectorAll("button")) {
      if (b.offsetParent === null) continue;
      const t = ((b.textContent || "") + " " + (b.getAttribute("aria-label") || "")).toLowerCase();
      if (/\b(submit|apply|continue|next|send application)\b/.test(t)) return true;
    }
    return false;
  }

  // Pages that are never an application form, whatever dialogs they render: the ZR
  // homepage (warmup hop) and the account pages both carry promo/nav dialogs that look
  // form-ish. Live 08-15: the warmup landing on /jobseeker/home was classified as "form",
  // so phase3 ran on it and logged an abandoned application for a job we never opened.
  const ZR_NON_JOB_PATH_RE = /^\/(jobseeker\/home|candidate\/|profile|account|settings)/;

  function findZipRecruiterApplyForm() {
    if (ZR_NON_JOB_PATH_RE.test(window.location.pathname)) return null;
    for (const d of document.querySelectorAll('[role="dialog"]')) {
      if (isZipRecruiterApplyForm(d)) return d;
    }
    return null;
  }

  async function waitForZipRecruiterForm(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (!(await isCampaignRunning())) return false;
      if (findZipRecruiterApplyForm()) return true;
      // A dialog that stays EMPTY is an external-apply job — but "empty" needs patience.
      // ZipRecruiter mounts the modal shell first and fills it in later, and in a
      // throttled background window that took ~30s twice on 08-21: we bailed at 2.5s
      // with "no ZR form after 15s", and the form then appeared 18 seconds after we had
      // already moved on. Give the shell real time before calling it external.
      const anyDialog = Array.from(document.querySelectorAll('[role="dialog"]')).some((d) => d.offsetParent);
      if (anyDialog && Date.now() - start > 12000) return false;
      // Redirected away to external ATS
      if (!window.location.hostname.includes("ziprecruiter.com")) return false;
      await sleep(500);
    }
    return false;
  }

  // =========================================================================
  // PHASE 3 — Application Form (multi-step)
  // =========================================================================

  // Not every application form is in English. Measured against 320 real Greenhouse forms
  // (data/gh_form_schemas.jsonl, scripts/form_coverage.py): Japanese, German, Dutch and
  // French forms left FIRST NAME, LAST NAME, EMAIL and PHONE blank — the four fields every
  // form requires — because the keyword map only spoke English. Matched on the RAW label,
  // since lower-casing does nothing for 姓 and the accents matter.
  const NAME_I18N_FIRST_RE = /^\s*(名|이름|vorname|voornaam|prénom|prenom|nombre|nome|imię|förnamn|fornavn|etunimi)\s*$/i;
  const NAME_I18N_LAST_RE = /^\s*(姓|성|nachname|familienname|achternaam|nom de famille|apellidos?|sobrenome|nazwisko|efternamn|etternavn|sukunimi)\s*$/i;
  const NAME_I18N_RE = new RegExp(`${NAME_I18N_FIRST_RE.source}|${NAME_I18N_LAST_RE.source}|^\\s*(氏名|お名前|naam|nom|nombre completo)\\s*$`, "i");
  const EMAIL_I18N_RE = /^\s*(電子メール|メールアドレス|이메일|e-?mail(adres|adresse)?|correo( electrónico)?|courriel|endereço de e-?mail)\s*$/i;
  const PHONE_I18N_RE = /^\s*(電話|電話番号|전화번호|telefon(nummer)?|telefoon(nummer)?|téléphone|telefone|teléfono|puhelin)\s*$/i;

  const LABEL_FALLBACKS = {
    firstName: "first name",
    lastName: "last name",
    email: "email",
    phone: "phone",
    coverLetter: "cover letter",
  };

  // Only real form controls can be typed into. A selector or a label can easily match a
  // DIV/SPAN/A — and then `el.value.trim()` throws, which killed the whole filler with no
  // log line at all (live 08-15 on a one-tap ZipRecruiter apply: the modal has no fields,
  // the search scope widened to the page, and an "email" selector matched page furniture).
  const isFormControl = (el) =>
    !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT");

  function findFieldBySelectorsOrLabel(fieldName) {
    // Scoped to the apply modal when one is open, same as every other filler.
    const scope = formScope();
    const fields = SELECTORS.fields || FALLBACK_SELECTORS.fields;
    const selectors = fields[fieldName] || [];
    for (const sel of selectors) {
      let el = null;
      try { el = scope.querySelector(sel); } catch { continue; } // a bad stored selector must not kill the run
      if (isFormControl(el) && el.offsetParent !== null) return el;
    }
    // Try label-based fallback
    const labelText = LABEL_FALLBACKS[fieldName];
    if (labelText) {
      const el = findByLabel(labelText);
      if (isFormControl(el)) return el;
    }
    return null;
  }

  // Fill unanswered radio-button screener questions.
  // Returns count of groups filled. Picks "Yes" for Yes/No groups (covers the
  // most common positive-eligibility questions); picks the first option otherwise.
  async function fillRadioQuestions() {
    const radios = formScope().querySelectorAll('input[type="radio"]');
    const seen = new Set();
    let filled = 0;
    // Read once, and only when there is a group to answer (work status needs the profile).
    let stored = null;
    const loadStored = async () => stored || (stored = await storageGet(["profile", "currentJobInfo"]));

    for (const r of radios) {
      // Nameless radios (React-controlled groups) can't be keyed by name — key each
      // element individually; once one in the scope is picked, the checked-guard
      // below skips the rest.
      const key = r.name || r;
      if (seen.has(key)) continue;
      seen.add(key);

      // `[name=""]` matches nothing, so a nameless radio made group = [] and
      // group[0].closest(...) threw — crashing the whole form fill. Fall back to
      // the enclosing fieldset/radiogroup, or the input alone.
      let group;
      if (r.name) {
        group = Array.from(document.querySelectorAll(`input[name="${r.name}"]`));
      } else {
        const scope = r.closest("fieldset, [role='radiogroup'], [role='group']");
        group = scope
          ? Array.from(scope.querySelectorAll('input[type="radio"]')).filter((o) => !o.name)
          : [r];
        if (!group.length) group = [r];
      }
      if (group.some((o) => o.checked)) continue; // already answered

      // Determine which option to pick
      const labels = group.map((o) => {
        const lbl = document.querySelector(`label[for="${o.id}"]`)?.textContent?.trim() ||
                    o.parentElement?.textContent?.trim() || "";
        return { el: o, lbl };
      });

      // Determine which option to pick
      const groupLabel = getFieldLabel(group[0].closest("fieldset, [role='radiogroup'], [role='group']") || group[0]);
      const optionTexts = labels.map((l) => l.lbl);
      let target = null;

      // Demographic / EEO radio groups (race, gender, veteran, disability incl.
      // Form CC-305) → pick the decline option, never a real identity value.
      const isDemo = isDemographicQuestion(groupLabel, optionTexts);
      if (isDemo) {
        target = (labels.find((l) =>
          /(decline|prefer not|don'?t wish|do not wish|not to (answer|say|disclose|identify)|rather not)/i.test(l.lbl)) || {}).el;
        // No decline option (e.g. only Yes/No for a disability question) → leave it
        // BLANK. These are legally voluntary; never fabricate a protected-class value
        // by falling through to the Yes/first-option default.
        if (!target) continue;
      }

      // Work authorization / visa sponsorship: the same decision as every other widget
      // (answerWorkStatus). This used to say "Yes" to any "authorized to work in …?" —
      // Canada and the UK included — and "No, I don't need sponsorship" to any visa
      // question without reading the profile. Unknown → blank, so a required group hands
      // the form back to the person instead of filing a guess about their legal status.
      if (!target) {
        const { profile = {}, currentJobInfo = {} } = (await loadStored()) || {};
        const status = await answerWorkStatus(groupLabel,
          labels.map((l) => ({ el: l.el, text: l.lbl })), profile || {}, currentJobInfo || {});
        if (status) {
          if (!status.pick) continue;
          target = status.pick.el;
        }
      }

      // Other eligibility ("18 or older", background check, "able to perform") and consent
      // to the employer's processing → affirmative. A bare "agree" no longer qualifies:
      // "Are you subject to any employment agreements…?" is a claim, not a consent.
      if (!target && (/(eligible|legally (permitted|able)|18 (years|or older)|over 18|able to (work|perform)|background check)/i.test(groupLabel) || isConsentToProcess(groupLabel))) {
        target = (labels.find((l) => /^yes\b/i.test(l.lbl)) || {}).el;
      }

      // The same knockouts the dropdown fallback refuses to guess ("Are you subject to a
      // non-compete…?", "Have you worked at <company>?"): the dropdown's own rule where it
      // has one ("previously worked at" → No on a cold application), else blank — never
      // the "Yes"/first-option default below.
      if (!target && isPersonalKnockout(groupLabel)) {
        const { profile = {} } = (await loadStored()) || {};
        const det = pickOptionDeterministic(groupLabel, labels.map((l) => ({ el: l.el, text: l.lbl })), profile || {});
        if (!det) continue;
        target = det.el;
      }

      if (!target) {
        // Generic fallback: exact "Yes" if present, else first option.
        const yesOpt = labels.find((l) => l.lbl.toLowerCase() === "yes");
        target = yesOpt ? yesOpt.el : group[0];
      }
      if (!target) continue;

      // Click via label if possible (React picks up the event better)
      const labelEl = document.querySelector(`label[for="${target.id}"]`);
      await humanClick(labelEl || target);
      filled++;
      await sleep(humanDelay(200, 500));
    }

    return filled;
  }

  // Tick required attestation / agreement / consent checkboxes. These block
  // submission (e.g. "I certify that I have read and understand…", Self Attestation)
  // and are always affirmations the applicant must accept to proceed. We do NOT touch
  // optional opt-ins (e.g. "email me about similar jobs") — only required boxes or
  // ones whose label clearly reads as an attestation/agreement.
  async function fillCheckboxes() {
    const boxes = Array.from(formScope().querySelectorAll('input[type="checkbox"]'))
      .filter((c) => c.offsetParent && !c.checked);
    let filled = 0;
    // The box's OWN label first: inside a fieldset getFieldLabel returns the legend (the
    // question), which would make every option read "Race / Ethnicity".
    const boxText = (c) => (c.id && document.querySelector(`label[for="${CSS.escape(c.id)}"]`)?.textContent) ||
      c.closest("label")?.textContent || getFieldLabel(c);
    // A demographic "select all that apply" group (race / ethnicity) gets its decline box,
    // the answer the radio and dropdown fillers already give. The generic loop below
    // refuses any label with "decline"/"don't" and skips unrequired members, so a required
    // race group was never answered and "Continue" was refused into a blind hand-back.
    // No decline option → left alone: never an identity value of ours.
    // A group is its OWN boxes and its OWN legend: an outer fieldset that wraps a nested
    // Race group plus an "I certify…" box must not inherit "Race" and swallow the attestation.
    const groupOf = (c) => c.closest("fieldset, [role='group']");
    const demoGroups = new Set();
    for (const g of new Set(boxes.map(groupOf).filter(Boolean))) {
      const all = Array.from(g.querySelectorAll('input[type="checkbox"]'))
        .filter((c) => c.offsetParent && groupOf(c) === g);
      const texts = all.map(boxText);
      const question = g.querySelector(":scope > legend")?.textContent || g.getAttribute("aria-label") || "";
      if (!isDemographicQuestion(question, texts)) continue;
      demoGroups.add(g);
      if (all.some((c) => c.checked)) continue;
      const i = texts.findIndex((t) => /(decline|prefer not|don'?t wish|do not wish|not to (answer|say|disclose|identify)|rather not)/i.test(t));
      if (i < 0) continue;
      const box = all[i];
      const labelEl = box.id ? document.querySelector(`label[for="${CSS.escape(box.id)}"]`) : null;
      await humanClick(labelEl || box);
      filled++;
      await sleep(humanDelay(200, 500));
    }
    // Work status needs the profile; read it only when such a box exists.
    let stored = null;
    const loadStored = async () => stored || (stored = await storageGet(["profile", "currentJobInfo"]));
    const statusByQuestion = new Map();
    for (const c of boxes) {
      if (demoGroups.has(groupOf(c))) continue;
      const label = getFieldLabel(c) ||
        (c.closest("label, [class*='question' i], fieldset")?.textContent || "");
      const own = String(boxText(c) || "").trim();
      const g = groupOf(c);
      const question = (g && (g.querySelector(":scope > legend")?.textContent || g.getAttribute("aria-label"))) || label;
      const mates = g ? Array.from(g.querySelectorAll('input[type="checkbox"]')).filter((b) => groupOf(b) === g) : [c];
      // A box reading just "Yes"/"No" is an ANSWER to the question above it, not a consent.
      const isYesNoBox = /^\W*(yes|no)\b/i.test(own);
      // Only the person can say they are a human and not a bot.
      if (PERSON_ONLY_RE.test(own) || PERSON_ONLY_RE.test(label)) continue;
      if (isYesNoBox && workStatus(question, {})) {
        // "Are you legally authorized to work in …?" drawn as Yes/No checkboxes (Greenhouse
        // multi-selects): `label` was the question for BOTH boxes and "authoriz" ticked
        // Yes and No alike. Same decision as every other widget, one box at most.
        if (!statusByQuestion.has(question)) {
          const { profile = {}, currentJobInfo = {} } = (await loadStored()) || {};
          statusByQuestion.set(question, await answerWorkStatus(question,
            mates.map((b) => ({ el: b, text: String(boxText(b) || "").trim() })), profile || {}, currentJobInfo || {}));
        }
        const status = statusByQuestion.get(question);
        if (!status || !status.pick || status.pick.el !== c) continue;
      } else if (workStatus(own, {}) || workStatus(label, {})) {
        // A statement box ("I am legally authorized to work in the United States"): ticked
        // only when the answer to it is Yes — never for another country on the US flag.
        const stmt = workStatus(own, {}) ? own : label;
        const { profile = {}, currentJobInfo = {} } = (await loadStored()) || {};
        const status = await answerWorkStatus(stmt, null, profile || {}, currentJobInfo || {});
        if (!status || !/^\W*yes\b/i.test(String(status.pick || ""))) continue;
      } else {
        const required = c.required || c.getAttribute("aria-required") === "true" ||
          c.getAttribute("aria-invalid") === "true";
        const isAffirmation = /certif|attest|agree|acknowledge|consent|i have read|i understand|\bterms\b|authoriz|confirm/i.test(label);
        // Never tick a NEGATIVE statement or an opt-in ("I do NOT consent…",
        // "I disagree…", "unsubscribe", "opt out") even if required/affirmation-worded.
        if (/\b(not|don'?t|do not|disagree|decline|unsubscribe|opt.?out|refuse)\b/i.test(label)) continue;
        if (isYesNoBox && mates.length > 1) {
          // A Yes/No choice: "Yes" only to a consent ("Do you acknowledge and agree to our
          // GDPR policy?"); a factual question ("Are you based in the NYC metro area?") is
          // the person's to answer — `required` used to tick Yes AND No.
          if (!(/^\W*yes\b/i.test(own) && isConsentToProcess(question))) continue;
        } else if (!required && !isAffirmation) continue;
      }
      const labelEl = c.id ? document.querySelector(`label[for="${CSS.escape(c.id)}"]`) : null;
      await humanClick(labelEl || c);
      filled++;
      await sleep(humanDelay(200, 500));
    }
    return filled;
  }

  // ── The cover letter: written when a form ASKS for one, not before ──────────────
  //
  // Measured 2026-09-23 (scripts/measure_letter_delivery.py, whole install): we generated
  // a letter on every application — 103 of 107 rows carry one — and typed it into a form
  // ONCE in 274 steps. Indeed never asks (its wizard is modular, and a cover-letter module
  // has not appeared in 214 form loads), while Greenhouse asks in 267 of the 320 schemas
  // we hold, behind a chooser the filler never opened. So we were paying for a letter the
  // employer never saw AND showing it in History as part of what we sent.
  //
  // Both halves are fixed here: generate lazily (the field is proof somebody asked) and
  // fill the field wherever it exists. `coverLetterFor` pins the text to a job so the key
  // can never hand the previous job's letter to this one.
  // Deliberately strict. "Why do you want to work here?" is a screener question the
  // answerer handles well; pasting a whole letter into it reads as a form-filler bot.
  const LETTER_LABEL_RE = /cover\s*letter|motivation(al)? letter/i;

  function coverLetterKeyFor(jobInfo) {
    const info = jobInfo || {};
    return `${(info.url || "").trim()}|${(info.title || "").trim().toLowerCase()}`;
  }

  // Returns the letter for the job we are applying to RIGHT NOW, generating it once.
  // Never throws: a form that asks for a letter must still be submittable if the model
  // is down — the template fallback (profile.writing_style) is what we had before.
  async function ensureCoverLetter() {
    const st = await storageGet(["generatedCoverLetter", "coverLetterFor", "currentJobInfo", "profile"]);
    const jobInfo = st.currentJobInfo || {};
    const key = coverLetterKeyFor(jobInfo);
    if (st.generatedCoverLetter && st.coverLetterFor === key) return st.generatedCoverLetter;

    let letter = "";
    logBackend(`✍️ Writing a cover letter — this form asks for one (${jobInfo.title || "this job"})`, "info");
    try {
      const res = await Promise.race([
        sendMsg({
          type: "GENERATE_COVER_LETTER",
          data: { job_title: jobInfo.title || "", company: jobInfo.company || "", description: jobInfo.description || "" },
        }),
        sleep(15000).then(() => ({ error: "timeout" })),
      ]);
      if (res && res.letter) letter = res.letter;
    } catch (e) {
      log("Cover letter error: " + e.message, "err");
    }
    if (!letter) {
      letter = (st.profile || {}).writing_style || "";
      logBackend("Cover letter generation failed — using your saved template", "warn");
    }
    // Stored against the job it was written for: the confirmation-page re-init (#238)
    // and the History record both read this key on a cold context.
    await storageSet({ generatedCoverLetter: letter, coverLetterFor: key });
    return letter;
  }

  // Greenhouse (and Ashby) hide the textarea behind a chooser — "Attach", "Dropbox",
  // "Google Drive", "Enter manually" — so the field is REAL but invisible until clicked.
  // That is why 267 of 320 schemas carry a Cover Letter question while only 9 of 51 live
  // form loads ever showed a textarea. Reveal it, then fill it.
  async function revealCoverLetterField() {
    const scope = formScope();
    const direct = findFieldBySelectorsOrLabel("coverLetter");
    if (direct) return direct;
    // Greenhouse names the trigger: data-testid="cover_letter-text" (the resume's is
    // resume-text), stable across boards — live zocdoc + affirm, 2026-09-25. Try it first,
    // then fall back to reading the buttons.
    const byTestId = Array.from(scope.querySelectorAll('[data-testid*="cover_letter" i], [data-testid*="coverletter" i]'))
      .filter((b) => b.offsetParent && /text|manual|write/i.test(b.getAttribute("data-testid") || ""));
    const byText = Array.from(scope.querySelectorAll('button, [role="button"], a')).filter((b) => {
      if (!b.offsetParent) return false;
      const t = (b.textContent || "").trim().toLowerCase();
      // "Enter manually" / "Write" / "Paste" — never "Attach"/"Upload": a file chooser
      // opens an OS dialog that would hang the run with nobody there to dismiss it.
      return /enter manually|type manually|write( it)? (here|manually)|paste/.test(t);
    });
    for (const btn of [...byTestId, ...byText]) {
      // Only inside the cover-letter block: the same chooser exists for the resume. Walk
      // UP until an ancestor actually names the field — closest("div") stopped at the
      // button's own wrapper, whose text is just "Enter manually", so every real
      // Greenhouse trigger was rejected (live: 6 forms, "no cover-letter field", 09-24).
      let block = btn, blockText = "";
      for (let hop = 0; hop < 6 && block; hop++) {
        blockText = (block.textContent || "").toLowerCase();
        if (/cover\s*letter/.test(blockText)) break;
        block = block.parentElement;
      }
      const named = (btn.getAttribute("data-testid") || "").toLowerCase().includes("cover")
        || /cover\s*letter/.test(blockText);
      if (!named) continue;
      await humanClick(btn);
      await sleep(humanDelay(400, 900));
      // The revealed textarea's own label reads "Enter manually", so the label test can
      // never identify it — take an empty visible textarea from inside the block we just
      // proved is the cover-letter one.
      const revealed = findFieldBySelectorsOrLabel("coverLetter")
        || (block && Array.from(block.querySelectorAll("textarea")).find((t) => t.offsetParent && !(t.value || "").trim()))
        || Array.from(scope.querySelectorAll("textarea")).find((t) => t.offsetParent && !(t.value || "").trim() && LETTER_LABEL_RE.test(getFieldLabel(t)));
      if (revealed) return revealed;
    }
    return null;
  }

  // Fill the cover-letter field if this form has one. Returns "" when the form has no
  // such field — the honest answer for Indeed, and what the application row then records.
  async function fillCoverLetterIfAsked(label, waitMs = 0) {
    let el = null;
    // ATS forms hydrate late: on a cold load the chooser is not in the DOM yet, and a
    // single look concluded "no field" on a form that has one (caught 2026-09-25 running
    // the real function against live Greenhouse pages — same URL missed cold, found warm).
    // Native wizards pass waitMs=0: Indeed has no such field to wait for.
    const deadline = Date.now() + Math.max(0, waitMs);
    do {
      try { el = await revealCoverLetterField(); } catch { /* a missing chooser must not kill the fill */ }
      if (el || Date.now() >= deadline) break;
      await sleep(1000);
    } while (!el);
    if (!el) {
      logBackend(`${label || platformLabel()}: no cover-letter field on this form — none written`, "info");
      return "";
    }
    if ((el.value || "").trim()) return el.value;
    const letter = await ensureCoverLetter();
    if (!letter) return "";
    quickSet(el, letter);
    await sleep(humanDelay(800, 1600));
    // READ IT BACK before claiming anything. quickSet returns true for "I assigned and
    // dispatched", not "the value stuck": a component that owns its own state can revert
    // the assignment on the next render. Claiming success here without looking would
    // rebuild the exact lie this whole path exists to remove — a durable "filled ✓" line
    // and an applications row saying the employer received a letter that isn't in the form.
    const landed = (el.value || "").trim();
    if (!landed) {
      logBackend(`${label || platformLabel()}: cover letter did NOT stick in the field — not recording it as sent`, "warn");
      return "";
    }
    // Durable on purpose: "the letter reached the form" is exactly the fact we could not
    // answer for three months, and the run log is where the next measurement reads it.
    logBackend(`${label || platformLabel()}: cover letter filled (${landed.length} chars) ✓`, "info");
    return landed;
  }

  // Fill required text/textarea screener fields that are empty.
  // Employer-defined screener questions can be any type — comments, name, date.
  // We infer the right value from the label text.
  async function fillTextQuestions() {
    const storageData = await storageGet(["profile", "currentJobInfo"]);
    const profile = storageData.profile || {};
    const jobInfo = storageData.currentJobInfo || {};
    const today = localDay();

    // Scope to the apply modal (see formScope) and DON'T demand a `required` marker.
    // ZipRecruiter's screener questions carry no required/aria-required attribute at all
    // (live 08-15: name="['573276']", required=false, label via label[for]) — so the old
    // required-only filter answered NOTHING, the step never validated, and phase3 clicked
    // "Continue" 20 times against an unchanged form before giving up. A visible, empty,
    // labelled question inside the apply form is a question we must answer.
    const scope = formScope();
    // Typed inputs count too. text/number/textarea was the whole list, so a field the
    // site declared as date/tel/email/url was invisible to the filler no matter how
    // well its label matched — live 09-23: Mach 1 Stores' "today's date" stayed blank,
    // the step validated against it, and "Continue" was refused 3× into a hand-back.
    // localDay() already returns YYYY-MM-DD, which is exactly what input[type=date] wants.
    // input:not([type]): Ashby's Location autocomplete is an <input> with NO type
    // attribute (live Suno form, 10-08) — it matched none of the typed selectors, so the
    // one filler that can drive a typeahead never saw it, fillComboboxes couldn't open a
    // menu without typing (hdSkip), and every required Ashby location blocked the submit.
    // :not([aria-hidden])/:not([tabindex="-1"]): Greenhouse react-selects ship a hidden
    // typeless <input required> mirror (…requiredInput, 14 on DoorDash's live form) —
    // typing into those burns the AI budget on invisible fields (#304's class) and makes
    // an unanswered dropdown look answered to collectUnfilledRequired.
    const inputs = Array.from(scope.querySelectorAll(
      'input[type="text"], input[type="number"], input[type="date"], ' +
      'input[type="tel"], input[type="email"], input[type="url"], ' +
      'input:not([type]):not([aria-hidden="true"]):not([tabindex="-1"]), textarea'))
      .filter(el => {
        if (!el.offsetParent || el.value.trim()) return false;
        if (el.type === "hidden" || el.readOnly || el.disabled) return false;
        // Never touch a site-search box that happens to live inside the scope.
        if (el.closest('form[role="search"]') || /search/i.test(el.name || "")) return false;
        // A react-select's search <input> stays "" after a choice; the answer is drawn next to it.
        if (reactSelectShownValue(el)) return false;
        return true;
      });

    let filled = 0;
    for (const el of inputs) {
      const rawLabel = getFieldLabel(el);
      const label = rawLabel.toLowerCase();
      const isTextarea = el.tagName === "TEXTAREA";
      // A dropdown's search box is not a text question. fillComboboxes owns it; here only
      // the typeaheads whose answer is a profile FACT we type (school, city/location).
      // Without this, a react-select yes/no burned an AI answer and got prose typed into it
      // (DoorDash ×2 hit the 15-answer budget on these alone, #304).
      const isCombo = isReactSelectField(el) || el.getAttribute("role") === "combobox";
      if (isCombo && !SCHOOL_FIELD_RE.test(rawLabel) && !/\bcity\b|\blocations?\b/.test(label)) continue;

      let value;
      let typeahead = "";
      const wsText = workStatus(rawLabel, profile);
      if (wsText) {
        // Legal work status FIRST, before any keyword rule: "Will you require sponsorship
        // (within 2 years)?" read as a "years" field and got "2", and "authorized to work
        // in any state?" as the state field. One decision for every widget
        // (answerWorkStatus): the US profile flag, else the person's own saved answer,
        // else blank. A yes/no-shaped label (or one about another country) only; an open
        // "What is your current visa status?" goes to the AI branch below, where the
        // backend applies the same rules.
        if (wsText.foreign || /^\W*(are|do|does|did|have|has|is|will|would|can|could|should|may)\b/i.test(rawLabel)) {
          const status = await answerWorkStatus(rawLabel, null, profile, jobInfo);
          if (!status || !status.pick) continue;
          value = status.pick;
        }
      // Only the applicant's OWN name — not "reference name", "company name",
      // "supervisor/manager/contact name" (those must go to the AI branch).
      } else if ((/\b(first|last|full|your|legal|preferred)\s+name\b|^name$/i.test(label) || NAME_I18N_RE.test(rawLabel)) &&
          !/(reference|company|employer|supervisor|manager|contact|emergency|previous|prior)/i.test(label)) {
        value = NAME_I18N_LAST_RE.test(rawLabel)
          ? (profile.last_name || "")
          : NAME_I18N_FIRST_RE.test(rawLabel)
            ? (profile.name || "")
            : `${profile.name || "Applicant"} ${profile.last_name || ""}`.trim();
      } else if (label.includes("date")) {
        value = today;
      } else if (/e-?\s?mail/.test(label) || EMAIL_I18N_RE.test(rawLabel)) {
        value = profile.email || "";
      } else if (label.includes("phone") || PHONE_I18N_RE.test(rawLabel)) {
        value = profile.phone || "";
      } else if (SCHOOL_FIELD_RE.test(rawLabel)) {
        // profile.school (asked at signup). "I don't have a college degree" is an answer
        // too: then a required school field hands back — never a university of ours.
        value = profile.no_degree ? "" : String(profile.school || "").trim();
        if (!value) continue;
        typeahead = "school";
      } else if (!isCombo && /^\W*(?:highest\s+)?degree(?:\s+(?:type|level|earned|obtained))?\W*$/i.test(rawLabel)) {
        value = profile.no_degree ? "" : String(profile.degree || "").trim();
        if (!value) continue;
      } else if (payQuestion(rawLabel) || label.includes("salary") || label.includes("compensation") || label.includes("pay") || label.includes("wage")) {
        // NEVER invent a number here. This used to read `profile.desired_salary || "65000"`,
        // a field that exists nowhere, so every user told employers 65000. salary_min is not
        // a substitute either: it filters which jobs to see, it states no expectation.
        // The answer is the user's own stated expectation (asked at signup,
        // profile.salary_expectation), shaped for the box: a bare number only where the
        // field's unit is the user's. Their CURRENT pay is another fact nobody told us.
        // No source => blank: validation blocks the step and the question goes back to the
        // human through the hand-back loop, the designed answer to "unknown".
        value = payQuestion(rawLabel) === "expectation"
          ? salaryAnswer(profile, null, el.type === "number", rawLabel) : "";
        if (!value) continue;
      } else if (label.includes("year") || label.includes("experience") || label.includes("how many") || label.includes("how long")) {
        // Only treat as a numeric "years" field for short inputs — an open textarea
        // asking about experience wants prose, which the AI branch handles below.
        if (!isTextarea) value = "2";
      } else if (label.includes("linkedin") || label.includes("portfolio") || label.includes("website") || label.includes("url") || label.includes("github")) {
        // Label-aware URL mapping: a LinkedIn question gets the LinkedIn URL, a
        // portfolio/website question gets the portfolio URL. New profile fields
        // linkedin_url/portfolio_url (old linkedin/portfolio kept as fallback).
        const li = profile.linkedin_url || profile.linkedin || "";
        const pf = profile.portfolio_url || profile.portfolio || "";
        if (label.includes("linkedin")) value = li;
        else if (label.includes("portfolio") || label.includes("website")) value = pf || li;
        else value = li || pf; // generic "url" / github
        if (!value) continue;
      } else if (/\b(street|address ?line|address ?1|mailing address)\b/.test(label) || label === "address") {
        value = profile.street_address || "";
        if (!value) continue; // never invent an address — hand back instead
      } else if (/\b(zip|postal)\b/.test(label)) {
        value = profile.postal_code || "";
        if (!value) continue;
      } else if (/\bstate\b|\bprovince\b|\bregion\b/.test(label)) {
        value = profile.state || "";
        if (!value) continue;
      } else if (/\bcommute\b/.test(label) && /^(are|will|can|do|would)\b/.test(label) && !isTextarea) {
        // "Will you be able to regularly commute and work in an office in job posting
        // location?" — a yes/no question whose label contains "location", so the city
        // rule below was answering it with "Miami, Florida, US" (live Braze form,
        // 2026-09-06). Jobs come from the user's own search location, so Yes is the
        // honest default. RELOCATION stays with the AI — "willing to relocate to
        // Qatar?" answered Yes deterministically could be a lie.
        value = "Yes";
      } else if (/(talent (community|network|pool)|newsletter|marketing (emails|communication)|future (job )?(opportunit|opening))/.test(label) && !isTextarea) {
        // Talent-community / newsletter opt-in — the platform marketing to the user,
        // not the employer asking about the candidate (same class as the SMS rule in
        // pickOptionDeterministic). Never subscribe the user without their consent.
        // Live Braze form 2026-09-06: this REQUIRED dropdown went blank and its
        // validation error silently blocked the whole submission.
        value = "No";
      } else if ((/\bcity\b|\blocations?\b/.test(label))
                 && !/^(are|do|does|did|have|has|is|was|were|would|will|may|can|should)\b/.test(label)) {
        // The user's real city if we have it; the search location is a fallback, not an
        // address (it can read "Miami, Florida, US" or even "remote").
        //
        // Word boundaries, not includes(): "capaCITY" and "reLOCATION" are not city
        // questions. The substring test was answering "are you open to relocation to
        // Qatar?" with "Miami" (6 required text questions across the 320 schemas; the
        // other 43 substring hits are selects, which never reach this chain).
        value = profile.city || profile.location || "Remote";
      // "recent"/"last" without "most": Indeed's own labels read "Recent job title" /
      // "Recent employer" (live 09-23, three Fieldhouse hand-backs) — the old pattern
      // required "most recent" and skipped them entirely, and at <20 chars they never
      // reached the AI branch either, so the step validated against blanks.
      } else if (/\b(current|most recent|recent|last|present)\b.*\b(employer|company|job title|title|position|role)\b/i.test(label)
                 && !/^(are|do|does|did|have|has|is|was|were|would|will|may|can|should)\b/i.test(label)
                 && !/how (are|do|did)|using|why|describe|reflect|scope/i.test(label)) {
        // Current employment. The single biggest hand-back cause on real forms: 12 of the
        // 21 required questions we left blank across 320 Greenhouse schemas were "current
        // company / employer / job title" (scripts/form_coverage.py), plus ~9 more that
        // each burned an AI call.
        //
        // The guards matter as much as the match. A yes/no-shaped label is never a
        // "name your employer" FIELD, it's a question ABOUT employment — "are you subject
        // to any employment agreements with your current employer?", "may we contact your
        // current employer?" — and would otherwise be answered with a company name. Same
        // for "how are you using AI in your current role?". Those belong to the AI branch.
        value = /\b(job title|title|position|role)\b/i.test(label)
          ? (profile.current_title || "")
          : (profile.current_employer || "");
        if (!value) continue; // never invent an employer — hand back instead
      } else if (/notice period|when (can|could) you start|available to start|start date/i.test(label) && !isTextarea) {
        value = profile.notice_period || "2 weeks";
      } else if (/(english|language).*(level|proficien|fluen)/i.test(label) && !isTextarea) {
        value = profile.english_level || "Fluent";
      } else if (LETTER_LABEL_RE.test(rawLabel)) {
        // The cover letter is not an open screener question: it already exists (or is
        // written on demand), and letting it fall through to the AI branch below charged
        // us a SECOND model call and answered "Cover Letter" as if it were a question —
        // a short screener reply instead of the letter written for this job.
        value = await ensureCoverLetter();
        if (!value) continue;
      }

      // Open-ended screener question the keyword rules can't map → ask the AI.
      // Gate to real questions (a textarea, or a label that reads like a question)
      // so we don't burn API calls on stray short inputs.
      if (value === undefined) {
        const looksLikeQuestion = isTextarea || rawLabel.includes("?") || rawLabel.length > 20;
        if (looksLikeQuestion && rawLabel && _aiAnswersUsed >= MAX_AI_ANSWERS_PER_FORM) {
          if (!_aiBudgetNotified) { logBackend(`Too many custom questions (>${MAX_AI_ANSWERS_PER_FORM}) — leaving the rest for you (faster than auto-answering all)`, "warn"); _aiBudgetNotified = true; }
          continue; // leave blank → validation-block → fast hand-back
        }
        if (looksLikeQuestion && rawLabel) {
          log(`AI answering screener: "${rawLabel.slice(0, 60)}"`, "");
          _aiAnswersUsed++;
          // Required fields BLOCK the whole application if left empty, so retry once
          // on an empty answer (a transient token refresh / network blip shouldn't
          // permanently stall the form). Optional fields get a single best-effort try.
          const maxTries = el.required || el.getAttribute("aria-required") === "true" ? 2 : 1;
          for (let attempt = 0; attempt < maxTries && (value === undefined || value === ""); attempt++) {
            if (attempt > 0) await sleep(humanDelay(1500, 2500));
            const res = await sendMsg({
              type: "ANSWER_QUESTION",
              data: { question: rawLabel, job_title: jobInfo.title || "", company: jobInfo.company || "" },
            });
            value = res && res.answer ? res.answer : undefined;
          }
        }
        if (value === undefined || value === "") {
          log(`Could not answer screener field: "${rawLabel || el.id || el.name}"`, "warn");
          continue;
        }
      }

      if (!value) continue;
      if (isReactSelectField(el) && typeahead === "school") {
        if (!(await fillSchoolTypeahead(el, value))) continue; // not offered → hand back
      } else if (isReactSelectField(el)) await fillReactSelect(el, value);   // P1c: GH/Lever location typeahead
      else if (isTextarea) quickSet(el, value);
      else setNativeValue(el, value);
      await sleep(humanDelay(150, 400));
      filled++;
    }
    return filled;
  }

  // ── Pay: only what the user told us to say (profile.salary_expectation) ──────────────
  // Ported from scripts/night_shift/common.py (pay_question / amounts / pay_unit /
  // salary_answer; cases in tests/test_night_shift_rules.py) so the browser and the server
  // answer pay the same way. The model once told employers $55k–$85k for a user whose own
  // floor was $100k; a figure of ours, or the NEAREST bracket, is that same lie.
  const PAY_SRC = String.raw`(?:salary|salaries|compensation|\bwages?\b|\bpay\b|\bpaid\b|\bOTE\b|\bincome\b|\bearnings\b|\bremuneration\b|(?:hourly|pay|day) rate|rate of pay)`;
  const PAY_WORD_RE = new RegExp(PAY_SRC, "i");
  const PAY_ASK_RE = new RegExp(
    String.raw`(?:expect\w*|desir\w*|requir\w*|target\w*|preferred|minimum|looking for|seeking)\b.{0,40}` + PAY_SRC +
    "|" + PAY_SRC + String.raw`.{0,40}\b(?:expect\w*|requir\w*|desir\w*|range|target\w*)` +
    String.raw`|how much (?:do|would|are) you (?:expect|want|like|need|looking)` +
    String.raw`|what (?:is|are) your (?:\w+ ){0,2}` + PAY_SRC, "i");
  const PAY_CURRENT_RE = new RegExp(String.raw`\b(?:current|present|most recent|last|previous)\b.{0,30}` + PAY_SRC, "i");
  const NOT_ABOUT_MY_PAY_RE = /\bexperience\b|\byears?\b|\bdescribe\b|\bhow many\b|\badministration\b|\bdesign\w*\b/i;
  const OPEN_ABOVE_RE = /\+|or more|and (?:up|above)|\babove\b|\bover\b|more than|at least/i;
  const OPEN_BELOW_RE = /\bunder\b|\bbelow\b|less than|up to/i;
  const PAY_UNITS = [
    ["hour", /\/\s*h(?:ou)?r\b|\bper hour\b|\bhourly\b|\ban hour\b|\bhrs?\b/i],
    ["month", /\/\s*mo(?:nth)?\b|\bper month\b|\bmonthly\b|\ba month\b/i],
    ["year", /\/\s*y(?:ea)?r\b|\bper year\b|\bper annum\b|\bannual\w*\b|\byearly\b|\ba year\b/i],
  ];

  // "expectation" | "current" | null (not about the candidate's pay at all).
  function payQuestion(label) {
    label = label || "";
    if (!PAY_WORD_RE.test(label) || NOT_ABOUT_MY_PAY_RE.test(label)) return null;
    if (PAY_CURRENT_RE.test(label)) return "current";
    return PAY_ASK_RE.test(label) || label.split(/\s+/).filter(Boolean).length <= 3 ? "expectation" : null;
  }

  // Every money figure in a phrase; a trailing k/m carries back over a range ("85-95k").
  function payAmounts(text) {
    const found = [];
    const re = /(\d[\d,]*(?:\.\d+)?)\s*([km])?\b/gi;
    let m;
    while ((m = re.exec(text || ""))) found.push([parseFloat(m[1].replace(/,/g, "")), (m[2] || "").toLowerCase()]);
    const scale = { k: 1000, m: 1000000, "": 1 };
    const out = [];
    found.forEach(([value, unit], i) => {
      if (!unit && value < 1000) unit = (found.slice(i + 1).find(([, u]) => u) || [0, ""])[1];
      if (value) out.push(Math.trunc(value * scale[unit]));
    });
    return out;
  }

  function payUnit(text, figures) {
    for (const [unit, re] of PAY_UNITS) if (re.test(text || "")) return unit;
    const f = figures == null ? payAmounts(text) : figures;
    if (f.length && Math.min(...f) >= 10000) return "year";
    if (f.length && Math.max(...f) <= 500) return "hour";
    return null;
  }

  // The user's stated expectation shaped for the control, or "" (nothing on file, chose
  // not to name one, or no bracket/unit that holds it — then a required field hands back).
  function salaryAnswer(profile, options, numeric, label) {
    const stated = String((profile && profile.salary_expectation) || "").trim();
    if (!stated || (profile && profile.no_salary_expectation)) return "";
    const figures = payAmounts(stated);
    const amount = figures.length ? figures[0] : null;
    const unit = payUnit(stated, figures);
    if (numeric) {
      const wanted = payUnit(label || "", []) || "year";
      return amount && unit === wanted ? String(amount) : "";
    }
    if (!options || !options.length) return stated;
    if (!amount || !unit) return "";
    // Half-open brackets ($75k–$100k then $100k–$125k puts $100k in the second), with one
    // inclusive pass for the top of the last one.
    for (const inclusive of [false, true]) {
      for (const option of options) {
        const bounds = payAmounts(option);
        if (!bounds.length || payUnit(option, bounds) !== unit) continue;
        const low = Math.min(...bounds), high = Math.max(...bounds);
        if (bounds.length >= 2 && low <= amount && (amount < high || (inclusive && amount === high))) return option;
        if (bounds.length === 1 && ((OPEN_ABOVE_RE.test(option) && amount >= low) ||
            (OPEN_BELOW_RE.test(option) && amount < low))) return option;
      }
    }
    return "";
  }

  // The school FIELD — its whole label. "Highest level of school completed" is a fixed list
  // and "Did you graduate from college?" a yes/no (same rule as night_shift _SCHOOL_FIELD).
  const SCHOOL_FIELD_RE = /^\W*(?:name of (?:your )?)?(?:school|university|college|institution)(?:\s*(?:\/|or|and)\s*(?:school|university|college|institution))?(?:\s+name)?\W*$/i;

  // The one option that IS the school, or null — never the nearest-looking one: the letters
  // of "MIT" are inside "Smith", and "University of Hawaii" matches every campus.
  function pickSchoolOption(query, optionTexts) {
    const squash = (t) => (t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const want = squash(query);
    if (!want || !optionTexts.length) return null;
    const exact = optionTexts.find((o) => squash(o) === want);
    if (exact) return exact;
    // Beyond exact, only the same NAME with a note in brackets: "University of Hawaii at
    // Manoa (Honolulu)". Never a word-subset either way — "Columbia College" (Missouri) is
    // not "Columbia College Chicago" (same rule as night_shift pick_typeahead).
    const unbracket = (t) => squash((t || "").replace(/\([^)]*\)|\[[^\]]*\]/g, " "));
    const base = unbracket(query);
    const near = base ? optionTexts.filter((o) => unbracket(o) === base) : [];
    return near.length === 1 ? near[0] : null;
  }

  // US states as a form lists them: by name ("Alabama", "(US) Alabama") or USPS code ("AL").
  // The profile holds either — the address seed copies the resume's "City, ST ZIP" line.
  const US_STATES = {
    AL: "Alabama", AK: "Alaska", AZ: "Arizona", AR: "Arkansas", CA: "California", CO: "Colorado",
    CT: "Connecticut", DE: "Delaware", DC: "District of Columbia", FL: "Florida", GA: "Georgia",
    HI: "Hawaii", ID: "Idaho", IL: "Illinois", IN: "Indiana", IA: "Iowa", KS: "Kansas",
    KY: "Kentucky", LA: "Louisiana", ME: "Maine", MD: "Maryland", MA: "Massachusetts",
    MI: "Michigan", MN: "Minnesota", MS: "Mississippi", MO: "Missouri", MT: "Montana",
    NE: "Nebraska", NV: "Nevada", NH: "New Hampshire", NJ: "New Jersey", NM: "New Mexico",
    NY: "New York", NC: "North Carolina", ND: "North Dakota", OH: "Ohio", OK: "Oklahoma",
    OR: "Oregon", PA: "Pennsylvania", RI: "Rhode Island", SC: "South Carolina", SD: "South Dakota",
    TN: "Tennessee", TX: "Texas", UT: "Utah", VT: "Vermont", VA: "Virginia", WA: "Washington",
    WV: "West Virginia", WI: "Wisconsin", WY: "Wyoming",
  };
  const stateKey = (t) => {
    const s = String(t || "").replace(/^\s*\(\s*(?:us|usa)\s*\)\s*/i, "").replace(/[^a-z ]+/gi, " ")
      .replace(/\s+/g, " ").trim().toLowerCase();
    if (US_STATES[s.toUpperCase()]) return s.toUpperCase();
    const code = Object.keys(US_STATES).find((c) => US_STATES[c].toLowerCase() === s);
    return code || "";
  };
  // A state-of-residence list (Greenhouse: 46–61 rows) → the profile's state, matched as a
  // whole name or code, or null. A substring put "HI" on Michigan; the first row put
  // Alabama on a Californian. `isList` tells chooseOption no model and no fallback either.
  function stateListPick(options, profile) {
    const keys = options.map((o) => stateKey(o.text));
    if (keys.filter(Boolean).length < 10) return { isList: false, option: null };
    const want = stateKey(profile.state);
    const hits = want ? options.filter((_, i) => keys[i] === want) : [];
    return { isList: true, option: hits.length === 1 ? hits[0] : null };
  }

  // Demographic / EEO self-identification — we auto-decline (most privacy-preserving,
  // and these are legally voluntary). Matches the question label OR the option set.
  function isDemographicQuestion(label, optionTexts) {
    const demo = /(gender|sex\b|race|ethnic|hispanic|latino|veteran|disab|sexual orientation|transgender|pronoun|national origin|self.?identif)/i;
    const hasDecline = optionTexts.some(t => /(decline|prefer not|don'?t wish|do not wish|not to (answer|say|disclose|identify)|rather not)/i.test(t));
    return demo.test(label) || (hasDecline && demo.test(optionTexts.join(" ")));
  }

  // ── Legal work status: ONE reading for every widget ──────────────────────────────────
  //
  // Radio groups, <select>s, comboboxes, text boxes and checkboxes each had their own copy
  // of "is this a work-authorization question, and what do we say". The radio copy said
  // "Yes" to ANY authorization question and "No, I don't need sponsorship" to any visa
  // question without reading the profile; the dropdown copy read `work_authorized_us`
  // but never the country. So "Are you legally authorized to work in Canada?" went out as
  // "Yes" under the user's name — a false statement about their legal status (28 of the
  // 320 real Greenhouse schemas ask about a country other than the US).
  //
  // Both profile flags (`work_authorized_us`, `needs_sponsorship`) are facts about the
  // UNITED STATES, and the product is US-only. So:
  //   US or no country named  → the profile flag, read the way the question points;
  //   another country/region  → never answered from the US flag. The person's own saved
  //                             answer for this job (hand-back loop, via the backend) or
  //                             nothing: blank, and a required field hands the form back.
  //   profile silent          → same as another country: the person's answer or blank.
  // Same rules as the backend's _status_from_profile (modules/ai_question_answer.py), which
  // is what the model path reaches: the two must agree, or a question gets two answers
  // depending on which widget it was drawn with.
  // Detection is wider than what the profile may answer: anything that smells of status
  // (a "work permit", "immigration support") must never fall to a "Yes"/first-option
  // default — it is answered from the profile only when the wording is one we can read.
  // "Can you legally work…", "Are you able to legally work…", "legally allowed / entitled
  // to work…", "allowed to work in…", "proof of eligibility to work…": the same
  // authorization question in other words. Until
  // 10-06 these fell through to the generic "eligible|legally|able to → Yes" rule, so a
  // profile that says "not authorized" told employers "Yes" (neo4j, cision, Indeed/ZR).
  const WS_AUTH_RE = new RegExp(
    "(authoriz|authoris|eligible|legally (permitted|authorized|able|allowed|entitled|eligible)|" +
    "lawfully|right to work|permanent work|work authoriz).{0,40}(work|employ)|" +
    "(work|employ).{0,40}(authoriz|authoris|eligible|legally|lawfully)|\\bright to work\\b|" +
    "\\b(legally|lawfully)\\s+(work|be employed)\\b|" +
    "\\b(entitled|eligibility)\\s+to\\s+(legally\\s+)?work\\b|\\b(allowed|permitted)\\s+to\\s+work\\s+(in|within|from)\\b", "i");
  // Smells of legal work status but is no wording we can answer from the two flags
  // ("work eligibility", "immigration status", "eligible … employment", OPT / CPT / EAD).
  // Such a question is "unclear" → blank, never left to a generic "Yes" default.
  const WS_STATUS_LOOK_RE = /\bwork (?:eligibility|status|rights?)\b|\b(?:employment|immigration|visa|residency) status\b|\b(?:legal(?:ly)?|lawful(?:ly)?)\b[^?]{0,40}\b(?:work|employ)|\beligib\w*\b[^?]{0,40}\b(?:work|employ|clearance)/i;
  // Student / visa-programme status (case-sensitive acronyms): the profile does not hold
  // it — "are you eligible for a 24-month OPT extension?" is not "authorized to work".
  const WS_VISA_PROGRAMME_RE = /optional practical training|curricular practical training|stem (?:opt )?extension/i;
  const WS_VISA_ACRONYM_RE = /\b(?:OPT|CPT|EAD)\b/;
  // "…work IN <place>": the place the question is about, when it says so.
  const WS_WORK_IN_RE = /\b(?:work|working|employment|employed)\b[^.?!]{0,60}?\b(?:in|within|from)\s+((?:the\s+)?[^.?!,;()]{2,60})/i;
  // "US"/"USA" only in capitals: lower-case "us" is the pronoun ("work for us in London").
  const WS_US_CAPS_RE = /\bU\.?S\.?A?(?![A-Za-z])/;
  const WS_US_WORDS_RE = /\bunited states\b|\bu\.\s?s\.|\bamerica\b(?!s)/i;
  // Mirrors modules/job_location.py (_NON_US_RE / _NON_US_CITY_RE / _NON_US_REGION_RE) —
  // top offenders, not a gazetteer. Re-sync when that list grows.
  const WS_NON_US_RE = new RegExp("\\b(" + [
    "canada|mexico|argentina|colombia|brazil|bulgaria|ireland|united kingdom|uk|england|germany|france|spain|portugal",
    "poland|romania|sweden|norway|denmark|finland|netherlands|belgium|switzerland|austria|italy|greece|turkey|israel",
    "india|pakistan|china|japan|korea|singapore|philippines|vietnam|thailand|indonesia|malaysia|australia|new zealand",
    "nigeria|kenya|egypt|south africa|ukraine|georgia \\(country\\)|armenia|kazakhstan",
    "chile|peru|uruguay|paraguay|bolivia|ecuador|venezuela|guatemala|honduras|nicaragua|costa rica|panama|el salvador",
    "dominican republic|czech republic|czechia|hungary|slovakia|slovenia|croatia|serbia|bosnia|lithuania|latvia|estonia",
    "belarus|moldova|cyprus|malta|iceland|luxembourg|united arab emirates|uae|dubai|abu dhabi|saudi arabia|qatar|kuwait",
    "bahrain|oman|taiwan|hong kong|bangladesh|sri lanka|nepal|myanmar|cambodia|laos|morocco|tunisia|algeria|ghana",
    "tanzania|uganda|ethiopia|senegal|rwanda|zimbabwe",
    // hub cities
    "bengaluru|bangalore|hyderabad|pune|mumbai|delhi|chennai|noida|gurgaon|gurugram|kolkata|ahmedabad|toronto|vancouver",
    "montreal|ottawa|london|manchester|edinburgh|berlin|munich|hamburg|paris|amsterdam|dublin|madrid|barcelona|lisbon",
    "warsaw|krakow|kraków|prague|budapest|bucharest|sofia|athens|stockholm|oslo|copenhagen|helsinki|zurich|geneva|vienna",
    "milan|rome|istanbul|tel aviv|tokyo|osaka|seoul|beijing|shanghai|shenzhen|taipei|manila|jakarta|kuala lumpur",
    "bangkok|hanoi|ho chi minh|sydney|melbourne|brisbane|auckland|wellington|s[ãa]o paulo|rio de janeiro|buenos aires",
    "santiago|bogot[áa]|lima|mexico city|monterrey|guadalajara|lagos|nairobi|cape town|johannesburg|kyiv|kiev|tbilisi",
    "yerevan|almaty|tashkent",
    // regions that are not the US and do not contain it
    "emea|apac|latam|eu|e\\.u\\.|europe|european(?: union)?|asia(?:[- ]pacific)?|latin america|south america",
    "central america|middle east|africa|oceania|nordics?|benelux|dach|anz|mena",
  ].join("|") + ")\\b", "i");
  // Wider than the US: "authorized to work in the Americas" is not answered by a US flag.
  const WS_WIDER_RE = /\b(?:the americas|north america|worldwide|globally)\b/i;

  // A sentence that tells the reader when NOT to answer ("(Skip this question if you are
  // applying to work in Canada or the UK). Do you … require sponsorship?" — 4 of the 320
  // schemas) names foreign places, but the question itself is the US one.
  function stripSkipClause(label) {
    return String(label || "")
      .replace(/\(\s*(?:please\s+)?(?:skip|ignore|disregard)\b[^)]*\)/gi, " ")
      .replace(/(?:^|[.!?]\s+)(?:please\s+)?(?:skip|ignore|disregard) this question if[^.?!]*[.!]/gi, " ");
  }

  // Places spelled with dots or with foreign-looking words, rewritten before the country
  // reading: "U.K." is the United Kingdom (its dots used to cut the place at "U", which
  // then read as the US), while "New Mexico", "New England", "Paris, Texas" and "Dublin,
  // Ohio/CA" are in the United States (they used to read as Mexico/England/France/Ireland).
  const WS_US_STATE_NAMES = "alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming";
  const WS_US_STATE_CODES = "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC";
  const WS_US_CITY_STATE_RE = new RegExp(
    "\\b[A-Z][A-Za-z.'-]*(?:\\s+[A-Z][A-Za-z.'-]*){0,2},\\s*(?:(?:" +
    WS_US_STATE_NAMES.split("|").map((n) => n.replace(/\b[a-z]/g, (c) => `[${c.toUpperCase()}${c}]`)).join("|") +
    ")\\b|(?:" + WS_US_STATE_CODES + ")\\b)", "g");
  function normalisePlaces(text) {
    return String(text || "")
      .replace(/\bU\.\s?S\.?(?:\s?A\.?)?(?![a-z])/gi, " United States ")
      .replace(/\bU\.\s?K\.?(?![a-z])/gi, " United Kingdom ")
      .replace(/\bE\.\s?U\.?(?![a-z])/gi, " European Union ")
      .replace(/\bnew\s+(?:mexico|england|jersey|york|hampshire)\b/gi, " United States ")
      .replace(WS_US_CITY_STATE_RE, " United States ");
  }
  // Not in job_location's list, which is about board locations rather than questions.
  const WS_UK_PARTS_RE = /\b(?:great britain|britain|scotland|wales|northern ireland)\b/i;
  function namesForeign(text) {
    return WS_NON_US_RE.test(text) || WS_UK_PARTS_RE.test(text) || WS_WIDER_RE.test(text);
  }

  function namesUS(text) {
    // "North/Latin/South America" are regions, not the US (WS_WIDER_RE / WS_NON_US_RE).
    const t = String(text || "").replace(/\b(north|latin|south|central)\s+america\b/gi, " ");
    return WS_US_CAPS_RE.test(t) || WS_US_WORDS_RE.test(t);
  }

  // Is this work-status question about somewhere other than the United States? Where the
  // question says "work in <place>", that place decides; a US mention elsewhere does not
  // rescue it, and a foreign one elsewhere ("e.g. TN for Canada/Mexico") does not sink a
  // US question. "the US or Canada" is answered by the US fact (either one is enough);
  // "both the US and Canada" is not. With no "work in", any foreign place and no US at all
  // is enough. A question that names nowhere is about the job's country — here, the US.
  function asksAboutAnotherPlace(question) {
    const q = normalisePlaces(question);
    const anchored = WS_WORK_IN_RE.exec(q);
    if (anchored) {
      const place = anchored[1];
      if (namesUS(place)) return namesForeign(place) && /\b(?:and|both)\b|&/i.test(place);
      if (namesForeign(place)) return true;
    }
    return namesForeign(q) && !namesUS(q);
  }

  // The question itself, without what surrounds it: parentheticals ("(e.g. H-1B)", "(we
  // can't sponsor visas)", "(Answer No if you can work without sponsorship)"), anything
  // after the first "?", and a note in front of it ("We cannot sponsor. Are you …?").
  // Which fact is asked, and which way Yes points, is read from this part only — a visa
  // note used to turn "Are you authorized to work in the US? (we can't sponsor visas)"
  // into a sponsorship question, and "…without sponsorship" in an explanatory clause
  // flipped "Do you require sponsorship?".
  function mainQuestion(label) {
    let t = normalisePlaces(stripSkipClause(label)).replace(/\([^)]*\)|\[[^\]]*\]/g, " ");
    const q = t.indexOf("?");
    if (q >= 0) t = t.slice(0, q + 1);
    const parts = t.split(/[.!;]\s+(?=[A-Z])/);
    return (q >= 0 ? parts[parts.length - 1] : t).replace(/\s+/g, " ").trim();
  }

  // What a work-status question asks, read from the main question:
  //   "auth"         — authorized / eligible / right to work / a valid work permit    → A
  //   "auth_without" — authorized WITHOUT sponsorship, for any employer, permanent or
  //                    unrestricted authorization ("…and do not require sponsorship")   → A && !N
  //   "sponsor"      — will you require / need sponsorship (or a visa, a work permit)    → N
  //   "unclear"      — a negated main verb ("Are you NOT authorized…?", "Do you not
  //                    require…?"), an either/or ("…without restriction, or will you
  //                    require sponsorship?"), or not a yes/no ("How long will you…")   → blank
  //   null           — not a work-status question.
  const WS_SPONSOR_WORDS_RE = /sponsor|visa\b|h-?1b|immigration|work permit/i;
  // "…will you (now or in the future) require/need…", "Does your work authorization
  // require <company> to sponsor…", "I will (not) require…" — anywhere in the question
  // ("In the country where you plan to work, will you … require … sponsorship?").
  const WS_REQ_START_RE = /\b(?:will|would|do|does|did)\s+(?:you|your\b[^?]{0,40}?)\b[^?]{0,80}?\b(?:require|need)s?\b|^\W*i\s+(?:will|would|do)\s+(?:not\s+)?(?:require|need)\b/i;
  // "Are you …" / "Do you have …" up front: an authorization question. If it ALSO asks
  // "will you require …", it is two questions in one → blank.
  const WS_AUTH_START_RE = /^\W*(?:[^:?]{0,60}[:,]\s*)?(?:are|is)\s+you\b|^\W*(?:[^:?]{0,60}[:,]\s*)?(?:do|does)\s+you\s+(?:currently\s+)?(?:have|hold|possess)\b/i;
  // Required: a work AUTHORIZATION one would need granted is sponsorship by another name
  // ("Will you require U.S. work authorization (e.g., visa sponsorship)…?").
  const WS_REQUIRED_THING_RE = /sponsor|visa\b|h-?1b|immigration|work permit|work authori[sz]ation/i;
  const WS_HOLDS_PERMIT_RE = /\b(?:have|hold|possess)\b[^?]{0,30}\bwork (?:permit|authori[sz]ation)\b/i;
  const WS_CAN_WORK_WITHOUT_RE = /\b(?:can|could|able to)\s+(?:legally\s+)?work\b[^?]{0,60}\bwithout\b/i;
  const WS_WITHOUT_RE = /\bwithout\b[^?]{0,60}\b(?:sponsor|visa)/i;
  const WS_AND_NO_SPONSOR_RE = /\band\s+(?:do not|don'?t|will not|won'?t|not)\s+(?:need|require)\b/i;
  const WS_UNRESTRICTED_RE = /permanent (?:work|employment) authori[sz]ation|\bunrestricted\b|for any (?:united states )?employer|without (?:any )?restrictions?|on a permanent basis|permanently/i;
  // Citizenship / permanent residence / a green card: facts the profile does not hold
  // (work_authorized_us is not citizenship) → always blank, never inferred.
  const WS_CITIZENSHIP_RE = /\bcitizen(?:s|ship)?\b|green card|permanent residen(?:t|ce|cy)|lawful permanent/i;
  const WS_WHICH_STATE_RE = /\b(?:which|what)\s+(?:u\.?s\.?\s+)?states?\b/i;
  const WS_NEGATED_RE = /\b(?:not|n't)\s+(?:currently\s+|yet\s+|legally\s+)*(?:authori[sz]ed|eligible|permitted|able to work|require|need)\b/i;
  const WS_EITHER_OR_RE = /\bor\s+(?:will|would|do|does)\s+you\s+(?:require|need)|\bor\s+(?:require|need)\b|\bor\s+(?:will|would|do)\s+you\s+(?:now\s+or\s+in\s+the\s+future\s+)?require/i;
  const WS_OPEN_RE = /^\W*(?:how|what|which|when|why|where)\b|\bplease\s+(?:describe|explain|list|specify|provide)\b/i;
  function workStatusClass(label) {
    const m = mainQuestion(label);
    // A paragraph with no question in it (an E-Verify notice, an "I certify…" block) is
    // not a work-status question, whatever words it uses; a statement box is short.
    if (!m.includes("?") && m.length > 150) return null;
    const auth = WS_AUTH_RE.test(m) || WS_HOLDS_PERMIT_RE.test(m) || WS_CAN_WORK_WITHOUT_RE.test(m);
    const sponsorWords = WS_SPONSOR_WORDS_RE.test(m);
    // "Are you a U.S. citizen or green card holder?", "…did you become a permanent resident
    // in any other country?" (Twitch; its "visas / work permits" sit after the "?") — read
    // on the FULL label, so a trailing clause can't drop it back to a Yes default.
    // "In which state do you hold permanent residency?" (Maven Clinic, a US state list) asks
    // WHERE the person lives — stateListPick answers it from the profile, not this block.
    if (!auth && WS_WHICH_STATE_RE.test(m)) return null;
    if (!auth && WS_CITIZENSHIP_RE.test(stripSkipClause(label))) return "unclear";
    // Looks like work status, but not a wording read below → blank, not a "Yes" default.
    const visaProgramme = WS_VISA_PROGRAMME_RE.test(m) || WS_VISA_ACRONYM_RE.test(m);
    if (!auth && !sponsorWords) return WS_STATUS_LOOK_RE.test(m) || visaProgramme ? "unclear" : null;
    if (WS_OPEN_RE.test(m)) return "unclear";
    const asksRequire = WS_REQ_START_RE.test(m);
    // On OPT / CPT / a STEM extension: a visa programme, not the authorization flag.
    if (!asksRequire && visaProgramme) return "unclear";
    if (asksRequire && WS_AUTH_START_RE.test(m)) return "unclear";
    if (asksRequire && WS_REQUIRED_THING_RE.test(m)) {
      // "Do you require sponsorship?" — unless the main verb is negated or "without".
      if (WS_NEGATED_RE.test(m) || WS_WITHOUT_RE.test(m)) return "unclear";
      return "sponsor";
    }
    if (auth) {
      if (WS_EITHER_OR_RE.test(m)) return "unclear";
      if (WS_WITHOUT_RE.test(m) || WS_AND_NO_SPONSOR_RE.test(m) || WS_UNRESTRICTED_RE.test(m)) {
        // "authorized … and do not require sponsorship" is a conjunction, not a negation.
        const rest = m.replace(WS_AND_NO_SPONSOR_RE, " ");
        return WS_NEGATED_RE.test(rest) ? "unclear" : "auth_without";
      }
      return WS_NEGATED_RE.test(m) ? "unclear" : "auth";
    }
    // Sponsorship words with no clear verb ("Sponsorship required?", "Visa sponsorship
    // needed now or in the future") → N; anything else ("Do you have an H-1B visa?") → blank.
    if (/\b(?:require|need)s?\b|\brequired\b|\bneeded\b/i.test(m) && !WS_NEGATED_RE.test(m) && !WS_WITHOUT_RE.test(m)) return "sponsor";
    return "unclear";
  }

  // null → not a work-status question (the caller's other rules apply).
  // Otherwise { kind, foreign, says } where `says` is the Yes/No that is TRUE for the
  // person (true = Yes), or null = we do not know it and must not guess. The two profile
  // flags are independent selects (Settings), so every combination is real — including
  // "not authorized" + "needs no sponsorship".
  function workStatus(label, profile) {
    const kind = workStatusClass(label);
    if (!kind) return null;
    if (asksAboutAnotherPlace(stripSkipClause(label))) return { kind, foreign: true, says: null };
    const p = profile || {};
    const A = typeof p.work_authorized_us === "boolean" ? p.work_authorized_us : null;
    const N = typeof p.needs_sponsorship === "boolean" ? p.needs_sponsorship : null;
    let says = null;
    if (kind === "auth") says = A;
    else if (kind === "sponsor") says = N;
    else if (kind === "auth_without") {
      if (A === false || N === true) says = false;
      else if (A === true && N === false) says = true;
    }
    return { kind, foreign: false, says };
  }

  // The option that says Yes (or No). Exactly one, or none: GitLab's sponsorship list has
  // seven "Yes, <visa type>" rows, and picking one would invent WHICH visa the person holds.
  function workStatusOption(says, options) {
    const want = says ? /^\W*yes\b/i : /^\W*no\b/i;
    const hits = options.filter((o) => want.test(String(o.text || "")));
    return hits.length === 1 ? hits[0] : null;
  }

  // The whole decision, for every widget. `options` = [{ text, ... }] or null for a text box.
  //   null           → not a work-status question;
  //   { pick: x }    → the option (or "Yes"/"No" text) to enter;
  //   { pick: null } → leave it blank: nobody has told us, and we never guess this one.
  // An unknown goes to the backend once: it returns the PERSON's own answer for this job
  // when the hand-back loop has one, and otherwise refuses ("") by the same rules as here.
  async function answerWorkStatus(label, options, profile, jobInfo) {
    const ws = workStatus(label, profile);
    if (!ws) return null;
    if (ws.says !== null) {
      if (!options) return { pick: ws.says ? "Yes" : "No" };
      const opt = workStatusOption(ws.says, options);
      if (opt) return { pick: opt };
    }
    if (_aiAnswersUsed >= MAX_AI_ANSWERS_PER_FORM) return { pick: null };
    _aiAnswersUsed++;
    const job = jobInfo || {};
    const res = await sendMsg({
      type: "ANSWER_QUESTION",
      data: {
        question: label,
        ...(options ? { options: options.map((o) => o.text) } : {}),
        job_title: job.title || "",
        company: job.company || "",
      },
    });
    const ans = res && res.answer ? String(res.answer).trim() : "";
    if (!ans) {
      logBackend(`Work-status question left for you${ws.foreign ? " (asks about a country other than the US)" : ""}: "${String(label).slice(0, 80)}"`, "warn");
      return { pick: null };
    }
    if (!options) return { pick: ans };
    const low = ans.toLowerCase();
    return { pick: options.find((o) => String(o.text || "").trim().toLowerCase() === low) || null };
  }

  // Consent the applicant gives to the EMPLOYER'S handling of the application — privacy
  // notices, data processing / retention, recording, a background check. Not a claim about
  // the person. The old pattern was the bare substrings "consent|agree", so "Are you
  // subject to any employment AGREEments with your current employer?" (5 of the 320
  // schemas) and "Are you currently employed … by Deloitte? … you agree that…" were
  // answered "Yes" — a non-compete and a past employer the person never had.
  const CONSENT_VERB_RE = /\b(consent(s|ing)?|agree|acknowledge|accept)\b/i;
  const CONSENT_OBJECT_RE = /privacy|personal (data|information)|\bdata\b|gdpr|ccpa|\bterms\b|conditions|polic(y|ies)|\bnotice\b|process(ing|es|ed)?\b|retain|retention|record|transcri|background (check|screen)|drug (test|screen)|e-?verify|reference check|use of (ai|artificial intelligence|automated)/i;
  // Being texted / marketed to is the platform's ask, not consent to process the
  // application: pickOptionDeterministic answers those "No".
  const MARKETING_RE = /text message|\bsms\b|opt.?in|newsletter|marketing|talent (community|network|pool)/i;
  // A question about the person ("Are you…", "Have you…", "Were you…") is a factual claim
  // whatever consent words it carries; so is anything about being a human and not a bot.
  const FACTUAL_CLAIM_RE = /^\W*(are|have|has|were|was|did|is)\s+you\b/i;
  const PERSON_ONLY_RE = /(real|actual) (human|person)\b|human being|\bnot (a |an )?(automated |ai )?(ro)?bot\b|automated (bot|tool|system|program|agent)/i;
  function isConsentToProcess(label) {
    const q = String(label || "");
    if (!CONSENT_VERB_RE.test(q) || !CONSENT_OBJECT_RE.test(q)) return false;
    if (FACTUAL_CLAIM_RE.test(q) || PERSON_ONLY_RE.test(q) || MARKETING_RE.test(q)) return false;
    return !workStatus(q, {});
  }

  // A yes/no about the person's own history that no position or default may answer:
  // visa status, having worked for this company, a restrictive agreement with an employer.
  // "How many years have you worked for a SaaS company" is about their experience, not
  // this company — benign, so it is not one of these.
  function isPersonalKnockout(label) {
    const q = String(label || "");
    return /sponsor|visas?\b|h-?1b|immigration|work permits?\b|green card|permanent residen|\bcitizen(?:s|ship)?\b/i.test(q) ||
      /non-?compet|non-?solicit|(employment|restrictive|post-employment) (agreements?|covenants?|restrictions?)|bound by any agreements?|subject to (any|a) [^?]{0,40}agreements?/i.test(q) ||
      (/(worked (at|for)|employed (by|at|with|for)|(former|previous|current) employee)/i.test(q) &&
       !/how (many|long)/i.test(q));
  }

  // Pick a dropdown option deterministically (no AI) for the common cases.
  // Returns the chosen option object, or null if it needs AI / a fallback.
  function pickOptionDeterministic(label, options, profile) {
    const texts = options.map(o => o.text);
    // Demographic → decline.
    if (isDemographicQuestion(label, texts)) {
      const decline = options.find(o =>
        /(decline|prefer not|don'?t wish|do not wish|not to (answer|say|disclose|identify)|rather not)/i.test(o.text));
      if (decline) return decline;
    }
    const yes = options.find(o => /^yes\b/i.test(o.text));
    const no = options.find(o => /^no\b/i.test(o.text));
    // Work authorization / sponsorship: the profile's answer, read the way the question
    // points and only for the US (workStatus). Anything else — another country, a silent
    // profile, an unclear wording — is null here; chooseOption's answerWorkStatus asks
    // for the person's own answer and otherwise leaves it blank. Never a default.
    const ws = workStatus(label, profile);
    if (ws) return ws.says === null ? null : workStatusOption(ws.says, options);
    // Marketing/SMS opt-in → No. It is the platform asking to text the user, not the
    // employer asking anything about the candidate, and nothing about the application
    // depends on the answer — so the least intrusive choice is the honest default.
    // (ZipRecruiter makes this one REQUIRED on its one-tap apply: name=['sms_opt_in'],
    // and a blank answer blocks the whole submission.)
    // "\bsms\b": the bare substring matched "mechaniSMS" / "organiSMS" and answered a real
    // question "No". ("opt.?in" never matched "option" — pinned in work-status.test.js.)
    if (/(text message|\bsms\b|opt.?in|receive (calls|messages|texts)|talent (community|network|pool)|newsletter)/i.test(label) && no) return no;
    // Previously worked at THIS company / referral-conflict → No (honest default for a
    // cold application; a real former employee reviews in TAP and can fix it).
    // "(ever|previously) been employed" requires a following by/at/with/for on purpose:
    // "have you ever been employed by Stripe?" is about THIS company (No is honest),
    // while "have you ever been employed in the securities industry?" is about the
    // candidate's own history — guessing No there could be a lie on a regulated form.
    if (/(previously (worked|employed)|ever worked (at|for)|(ever|previously) been employed (by|at|with|for)|former (employee|employer)|currently employed by)/i.test(label) && no) return no;
    // English / language proficiency → the strongest fluency option present.
    if (/(english|language).*(level|proficien|fluen)|(level|proficien).*(english|language)/i.test(label)) {
      const fluent = options.find(o => /(native|fluent|full professional|advanced|c2|c1)/i.test(o.text));
      if (fluent) return fluent;
    }
    // How did you hear about us → a neutral truthful source. "(first )?" covers the
    // "how did you first learn about X as an employer?" phrasing (4× in the 320 schemas).
    if (/how did you (first )?(hear|find|learn)|hear about (this|us|the)/i.test(label)) {
      const src = options.find(o => /(job board|linkedin|company (website|careers)|online|internet|other)/i.test(o.text));
      if (src) return src;
    }
    // Country / residence select → United States when the profile is US-based.
    if (/(country|where (do|will) you (reside|live|work)|located in)/i.test(label)) {
      const us = options.find(o => /(united states|usa|u\.s\.)/i.test(o.text));
      if (us && /^\+?1|us|remote/i.test(String(profile.phone || profile.location || ""))) return us;
    }
    // Yes/No eligibility that is not legal work status (18+, background check, "legally
    // able to drive") and consent to the employer's processing → Yes. Work status was
    // decided above; a bare "agree" is no longer enough (isConsentToProcess).
    if (yes && (/(eligible|18|over 18|legally|background|able to)/i.test(label) || isConsentToProcess(label))) {
      return yes;
    }
    // Salary bracket → the one that HOLDS the user's stated figure (salaryAnswer), never the
    // nearest. This read `profile.desired_salary`, a field that exists nowhere, so it never
    // fired. No bracket holds it → chooseOption leaves the field blank (see there).
    if (payQuestion(label) === "expectation") {
      const pick = salaryAnswer(profile, texts, false, label);
      if (pick) return options.find(o => o.text === pick) || null;
    }
    return null;
  }

  // Fill required/empty <select> dropdowns. Deterministic for demographic, Yes/No,
  // and salary; AI for ambiguous; first real option as a last resort so a required
  // dropdown can never stall the whole application.
  async function fillSelectQuestions() {
    const storageData = await storageGet(["profile", "currentJobInfo"]);
    const profile = storageData.profile || {};
    const jobInfo = storageData.currentJobInfo || {};

    const selects = Array.from(formScope().querySelectorAll("select")).filter(s => {
      if (!s.offsetParent) return false;
      const cur = (s.options[s.selectedIndex]?.textContent || "").trim();
      // Only fill if still on a placeholder / empty selection.
      return !s.value || /^(select|choose|please|--|\s*)$/i.test(cur) || /select an option|please select/i.test(cur);
    });

    let filled = 0;
    for (const sel of selects) {
      const label = getFieldLabel(sel);
      const options = Array.from(sel.options)
        .map(o => ({ el: o, text: (o.textContent || "").trim(), val: o.value }))
        .filter(o => o.val && !/^(select|choose|please|--)/i.test(o.text));
      if (!options.length) continue;

      const chosen = await chooseOption(label, options, profile, jobInfo);
      if (!chosen) continue;
      setSelectValue(sel, chosen.val);
      filled++;
      await sleep(humanDelay(300, 700));
    }
    return filled;
  }

  // Shared option chooser: deterministic (demographic/Yes-No/salary) → AI → a SAFE
  // fallback. Never blind-picks options[0] — that could send a wrong/harmful answer
  // to an employer (e.g. "Male" on a label-less gender dropdown, or "No" on a
  // reordered right-to-work question). Prefers a neutral option, then affirmative
  // for eligibility, and only falls to the first option for clearly-benign dropdowns.
  async function chooseOption(label, options, profile, jobInfo) {
    // Legal work status is decided in one place for every widget — never by the model's
    // guess or a fallback below (see answerWorkStatus).
    const status = await answerWorkStatus(label, options, profile, jobInfo);
    if (status) return status.pick;
    let chosen = pickOptionDeterministic(label, options, profile);
    // Pay is the user's figure or nothing: no model, and none of the fallbacks below (the
    // "first real option" one would put a bracket of ours on the application).
    if (payQuestion(label)) return chosen || null;
    // Where the user lives is the profile's state or nothing — same reasoning.
    const stateList = stateListPick(options, profile);
    if (stateList.isList) return stateList.option;
    if (!chosen && _aiAnswersUsed >= MAX_AI_ANSWERS_PER_FORM) {
      if (!_aiBudgetNotified) { logBackend(`Too many custom questions (>${MAX_AI_ANSWERS_PER_FORM}) — leaving the rest for you (faster than auto-answering all)`, "warn"); _aiBudgetNotified = true; }
      // fall through to the SAFE no-AI fallbacks below (neutral/eligibility/blank)
    } else if (!chosen) {
      log(`AI picking dropdown: "${label.slice(0, 50)}"`, "");
      _aiAnswersUsed++;
      const res = await sendMsg({
        type: "ANSWER_QUESTION",
        data: {
          question: label,
          options: options.map(o => o.text),
          job_title: jobInfo.title || "",
          company: jobInfo.company || "",
        },
      });
      const ans = res && res.answer ? String(res.answer).trim().toLowerCase() : "";
      if (ans) chosen = options.find(o => o.text.trim().toLowerCase() === ans);
    }
    if (chosen) return chosen;

    // Safe fallbacks (no confident answer):
    // 1) a neutral/decline option is harmless for ANY question type (incl. an
    //    unlabelled demographic dropdown) → prefer it.
    const neutral = options.find(o =>
      /(prefer not|decline|do not wish|don'?t wish|rather not|^n\/?a$|not applicable|^other$|^none$)/i.test(o.text));
    if (neutral) return neutral;
    // 2) a knockout about the person — visa sponsorship, having worked for this company —
    //    is answered from the profile or the model, never by position or by the
    //    eligibility rule below (DoorDash's sponsorship question says "eligibility" and
    //    "authorization", which step 3 read as "say Yes"). The first option on
    //    DoorDash's sponsorship questions is "Yes", on "Have you worked at DoorDash?" it is
    //    "I am a previous employee" (live 10-06). Blank → hand-back, the person answers.
    //    "How many years have you worked for a SaaS company" is about the person's history,
    //    not this company — benign, so it keeps the fallbacks below.
    //    Restrictive agreements too: "Are you subject to any non-compete / employment
    //    agreements with your current employer?" (5 of the 320 schemas; Scale AI's first
    //    option is "Yes") — a "Yes" there tells the employer you are bound.
    if (isPersonalKnockout(label)) return null;
    // 3) eligibility / yes-no phrasing → affirmative, never a stray first option. Not
    //    work status (answered above) and not a bare "agree": consent to processing only.
    if (/(eligible|legally|able to|18 (years|or older)|over 18|background)/i.test(label) || isConsentToProcess(label)) {
      const yes = options.find(o => /^yes\b/i.test(o.text));
      if (yes) return yes;
    }
    // 4) a demographic-looking option set with no neutral → leave unfilled rather
    //    than fabricate an identity value.
    if (/(male|female|non.?binary|hispanic|latino|black|white|asian|veteran|disab)/i.test(options.map(o => o.text).join(" "))) {
      return null;
    }
    // 5) genuinely benign dropdown → first real option.
    return options[0];
  }

  // Fill custom (non-native) dropdowns — Indeed renders demographic/screener
  // dropdowns as DIVs with role="combobox", not <select>. We click to open, read
  // the role="option" list (often portaled), pick, and click. This is what was
  // stalling the demographic page ("Choose an option to continue").
  async function fillComboboxes() {
    const storageData = await storageGet(["profile", "currentJobInfo"]);
    const profile = storageData.profile || {};
    const jobInfo = storageData.currentJobInfo || {};

    // "Has this widget been answered?" — by TEXT alone this is unknowable across
    // platforms. ZipRecruiter renders its yes/no screeners as DIV[role=combobox] whose
    // text is "Open" (the disclosure affordance), so a placeholder-text test called them
    // answered and skipped every one — live 08-15 on Chiquita Brands: two text fields
    // filled, three unanswered questions ("legally authorized to work in the US",
    // "drug screen", "background check"), and "Continue" refused until we handed the job
    // back. Ask the widget for its VALUE instead, and only fall back to text.
    const comboValue = (c) => {
      // Greenhouse react-select: the search <input> stays "" and the answer is drawn in a
      // sibling .select__single-value (live twilio/8170555, #307).
      const rs = reactSelectShownValue(c);
      if (rs) return rs;
      const active = c.getAttribute("aria-activedescendant");
      if (active) {
        const el = document.getElementById(active);
        if (el && (el.textContent || "").trim()) return (el.textContent || "").trim();
      }
      const selected = c.querySelector('[aria-selected="true"], [class*="singleValue"], [class*="select__single-value"]');
      if (selected && (selected.textContent || "").trim()) return (selected.textContent || "").trim();
      // Widgets that mirror their answer into a hidden/associated input.
      const name = c.getAttribute("name");
      if (name) {
        const mirror = document.querySelector(`input[name="${CSS.escape(name)}"]`);
        if (mirror && (mirror.value || "").trim()) return mirror.value.trim();
      }
      if (c.tagName === "INPUT" && (c.value || "").trim()) return c.value.trim();
      return "";
    };

    const PLACEHOLDER_RE = /^(select|choose|please|--)?\s*(an?\s+)?option?$|^\s*$|select an option|please select|^select$|^choose$|^open$/i;

    const isUnfilled = (c) => {
      if (!c.offsetParent || c.dataset.hdSkip || c.dataset.hdDone) return false;
      if (comboValue(c)) return false; // genuinely answered
      const txt = (c.textContent || "").trim();
      return PLACEHOLDER_RE.test(txt) || txt.length <= 24; // short label = affordance, not an answer
    };

    const COMBO_SEL = '[role="combobox"], button[aria-haspopup="listbox"], [class*="select__control"]';
    let filled = 0;
    // Re-query each pass instead of iterating a captured snapshot: a React re-render
    // after filling one combobox can detach the others, so cached nodes would no-op.
    // data-hd-skip marks un-openable ones so we don't loop on them forever.
    // The pass budget scales with the form: one pass per attempt, up to 2 attempts per
    // widget. A flat 14 ran out on DoorDash's Greenhouse form (16 react-selects, live
    // 10-05): "combo×14", then the last one on the page — Disability Status, required —
    // was never opened and both applications were handed back.
    const maxPasses = Math.min(80, Math.max(14, 2 * formScope().querySelectorAll(COMBO_SEL).length));
    for (let pass = 0; pass < maxPasses; pass++) {
      // Scoped to the apply modal like every other filler. Unscoped, this reached the
      // BOARD's own controls: on a one-tap ZipRecruiter job the modal holds no fields, so
      // the scope falls back to the document, and the page's filter chips (Remote, Date
      // posted, Experience level, Distance) look exactly like unanswered dropdowns. Live
      // 22:56 on Subway "Manager, Social & Activation": the engine opened the apply modal,
      // then spent 36 seconds operating the search filters behind it, which re-rendered
      // the results and closed the modal. No application, no log line, nothing.
      // Indeed DIV combobox + native ARIA listbox buttons + react-select controls
      // (Greenhouse/Lever new UI render multi_value_single_select as react-select,
      //  invisible to querySelectorAll('select') — 2026-08-09 #11 detect-gap).
      const combo = Array.from(formScope().querySelectorAll(COMBO_SEL)).find(isUnfilled);
      if (!combo) break;

      const label = getComboLabel(combo);
      await openCombobox(combo);

      // Read options from THIS combobox's OWN menu — a global [role=option] query
      // could grab a different question's still-open menu and apply its answer here.
      const menu = findComboMenu(combo);
      const rawOpts = menu ? Array.from(menu.querySelectorAll('[role="option"], li, [class*="select__option"]')) : [];
      const txt = (o) => (o.textContent || "").trim();
      const optEls = rawOpts
        // Innermost only: `li > [role=option]` returns both with the same text, parent first,
        // and a click on the outer one never reaches a handler bound on the inner.
        .filter(o => !rawOpts.some(p => p !== o && o.contains(p) && txt(p) === txt(o)))
        // Same reason as findComboMenu: a real option can report offsetParent null.
        // Require only that it is not display:none.
        .filter(o => o.getClientRects().length > 0 || txt(o));
      const options = optEls
        .map(o => ({ el: o, text: (o.textContent || "").trim(), val: (o.textContent || "").trim() }))
        .filter(o => o.text && !/^(select|choose|please|--)/i.test(o.text));

      if (!options.length) {
        // Couldn't open / read it — mark to skip and move on (don't re-loop).
        combo.dataset.hdSkip = "1";
        combo.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        continue;
      }

      const chosen = await chooseOption(label, options, profile, jobInfo);
      if (!chosen) {
        combo.dataset.hdSkip = "1";
        combo.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        continue;
      }

      const before = (combo.textContent || "").trim();
      await humanClick(chosen.el);
      await sleep(humanDelay(400, 800));
      // Did the widget TAKE it? A readable value, a visible change off the placeholder, or a
      // re-render that replaced the node (the next pass re-reads the new one). Counting the
      // click alone made a choice that never committed report combo×1, then vanish from
      // every later pass: filled=[] → hand-back with nothing named (Indeed demographic ×3).
      const after = (combo.textContent || "").trim();
      const took = !combo.isConnected || !!comboValue(combo) ||
        (after !== before && !PLACEHOLDER_RE.test(after));
      const tries = Number(combo.dataset.hdTries || 0) + 1;
      combo.dataset.hdTries = String(tries);
      if (took) {
        // Monotonic: a widget that shows its answer as plain short text ("Yes") would
        // otherwise look unanswered on the next pass and be re-opened forever.
        combo.dataset.hdDone = "1";
        filled++;
      } else if (tries >= 2) {
        combo.dataset.hdSkip = "1";
        combo.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        logBackend(`Dropdown didn't take our choice: "${label.slice(0, 60)}" → "${chosen.text.slice(0, 40)}"`, "warn");
      }
      // else: still unfilled → the next pass re-finds it and tries once more.
    }
    return filled;
  }

  // Find the listbox menu that belongs to a specific combobox (portaled or inline).
  // Downshift (ZipRecruiter's dropdown library) opens on ArrowDown, not on a synthetic
  // click: dispatching mouse events left aria-expanded="false" and no menu, every time.
  // Click first anyway for the libraries that DO listen for it, then key it open.
  async function openCombobox(combo) {
    await humanClick(combo);
    await sleep(humanDelay(250, 500));
    if (combo.getAttribute("aria-expanded") === "true") return;
    try { combo.focus(); } catch { /* not focusable */ }
    for (const type of ["keydown", "keyup"]) {
      combo.dispatchEvent(new KeyboardEvent(type, {
        key: "ArrowDown", code: "ArrowDown", keyCode: 40, which: 40,
        bubbles: true, cancelable: true,
      }));
    }
    await sleep(humanDelay(300, 600));
  }

  function findComboMenu(combo) {
    const id = combo.getAttribute("aria-controls") || combo.getAttribute("aria-owns");
    if (id) {
      const el = document.getElementById(id);
      // NO offsetParent test here. ZipRecruiter's Downshift menus are positioned so the
      // <ul> reports offsetParent === null while holding perfectly real, clickable
      // <li role="option"> children — live 08-21 that made the menu invisible to us and
      // every ZR yes/no question went unanswered.
      if (el && el.children.length) return el;
    }
    // Fallback: the most-recently-opened visible listbox / react-select menu.
    const lbs = Array.from(
      document.querySelectorAll('[role="listbox"], [class*="select__menu"]')
    ).filter(l => l.offsetParent !== null);
    return lbs.length ? lbs[lbs.length - 1] : null;
  }

  // Label the page itself ties to this element — aria-labelledby, label[for], aria-label —
  // and nothing guessed from the surrounding block.
  function explicitLabel(el) {
    if (!el) return "";
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    const byIds = ids.map(id => document.getElementById(id)?.textContent || "").join(" ");
    const byFor = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.textContent : "";
    return (byIds || byFor || el.getAttribute("aria-label") || "").replace(/[\u200b-\u200d\ufeff]/g, "").replace(/\s+/g, " ").trim();
  }

  // Label for a custom combobox. The page's own label wins: a Greenhouse react-select
  // control is a bare DIV, its label belongs to the input[role=combobox] inside it.
  // Without that, getFieldLabel walked up to the shared wrapper and took ITS first label:
  // on DoorDash (live 10-06) Country read "Phone" (the phone fieldset's legend), Location
  // read "First Name", and work authorization, both visa-sponsorship questions and "Have
  // you worked at DoorDash?" all read "LinkedIn Profile*" — so the profile's sponsorship
  // answer never applied and the first option ("Yes") went out instead.
  // Otherwise: text of the enclosing question block minus the combobox's own placeholder.
  function getComboLabel(combo) {
    const own = combo.matches('[role="combobox"]') ? combo : combo.querySelector('[role="combobox"]');
    const named = explicitLabel(own);
    if (named && !/^[-\s]*(select|choose|please select)( an?| one)?( option)?[\s.…-]*$/i.test(named)) return named;
    const direct = getFieldLabel(combo);
    if (direct && !/select an option|choose|please select/i.test(direct)) return direct;
    const container = combo.closest("[class*='question' i], fieldset, [role='group'], li, div");
    if (container) {
      const comboText = (combo.textContent || "").trim();
      const full = (container.textContent || "").replace(comboText, "").replace(/\s+/g, " ").trim();
      if (full) return full.slice(0, 200);
    }
    return direct || "";
  }

  // The visible apply modal, if the platform renders the form in one (Indeed's in-page
  // modal, ZipRecruiter's Quick Apply). Button lookup must be scoped to it: the page
  // BEHIND the modal keeps its own visible buttons (ZR right-pane "Quick Apply", card
  // badges, nav) that a document-wide text match picks up first in DOM order.
  // All visible dialogs, the ones holding form fields first. ZR keeps MORE THAN ONE
  // visible [role=dialog] mounted while Quick Apply is open (live 08-15: the first one
  // has buttons but no fields; the apply form is a later sibling) — "first dialog with
  // a button" picked the wrong one and the lookup fell through to the page.
  // "Has fields" must include CUSTOM widgets, not just native controls. ZipRecruiter's
  // required SMS opt-in is a DIV[role=combobox] — with a native-only test the consent
  // modal counted as field-less, the scope fell to the empty wrapper dialog next to it,
  // and the filler never saw the one control the step required. Live 08-15: "Continue"
  // clicked twice, "This field is required" on screen, aria-invalid=true on
  // name=['sms_opt_in'], and the application handed back.
  const FIELDISH_SELECTOR =
    'input, textarea, select, [role="combobox"], [role="checkbox"], [role="radio"], [role="switch"], [contenteditable="true"]';

  function visibleApplyDialogs() {
    const all = Array.from(document.querySelectorAll('[role="dialog"]')).filter((d) => d.offsetParent !== null);
    const withFields = all.filter((d) => d.querySelector(FIELDISH_SELECTOR));
    const rest = all.filter((d) => !withFields.includes(d) && d.querySelector("button"));
    return withFields.concat(rest);
  }
  function visibleApplyDialog() { return visibleApplyDialogs()[0] || null; }

  // The element that owns the current application step. When the platform renders the
  // apply flow in a modal (Indeed, ZipRecruiter) the fillers MUST stay inside it: the
  // page behind keeps its own inputs and buttons (ZR's header search box is the famous
  // one) and a document-wide query reaches them first.
  function formScope() {
    const dlgs = visibleApplyDialogs();
    // Prefer the dialog that holds fields; otherwise ANY open apply dialog. Falling back
    // to `document` just because the modal has no inputs is what let the fillers loose on
    // the board's own search filters during a one-tap ZipRecruiter apply (live 08-15).
    return dlgs.find((d) => d.querySelector(FIELDISH_SELECTOR)) || dlgs[0] || document;
  }

  // Compact "what modals are on screen" string — the one fact that told us WHY a ZR
  // apply died. Cheap enough to attach to every give-up path.
  function dialogSnapshot() {
    const dlgs = visibleApplyDialogs();
    if (!dlgs.length) return "dialogs=0";
    return `dialogs=${dlgs.length} ` + dlgs.slice(0, 3).map((d, i) => {
      const ins = d.querySelectorAll("input, textarea, select").length;
      const bts = Array.from(d.querySelectorAll("button")).filter((b) => b.offsetParent !== null)
        .map((b) => (b.textContent || b.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim())
        .filter(Boolean).slice(0, 5).join("|");
      return `[${i}:in=${ins} btn=${bts}]`;
    }).join(" ");
  }

  // What the PAGE says is wrong when it refuses a step: its validation messages, the fields
  // it marks invalid or required-and-empty, the step path and the button state. Without it
  // 17 Indeed hand-backs (09-06…10-01) read "nothing left to fill" with no clue, and on a
  // page-level form dialogSnapshot() is literally "dialogs=0". Field `.value` is never read
  // (Indeed's structured-data-review step renders the user's parsed resume); page messages
  // are kept, with emails and phone-like numbers masked in case a message echoes input.
  // Diagnostics leave the page as text in the activity log. Labels and headings can echo the
  // person (an account-menu "igor@…", a contact card's phone), so every diag string goes
  // through this before it is kept.
  function maskPii(s) {
    return (s || "").replace(/\S+@\S+\.\S+/g, "<email>").replace(/\+?\d[\d\s().-]{5,}\d/g, "<num>");
  }

  function formBlockers() {
    try {
      const { btn, label } = classifyFormButton();
      let scope = formScope();
      const pageLevel = scope === document;
      // Page-level form: the form holding the step's button, else <main>. NOT the first of
      // "main, form" in document order — a header search <form> comes first on many boards.
      if (pageLevel) {
        const own = btn && btn.closest("form");
        scope = (own && own.querySelector(FIELDISH_SELECTOR) && own) ||
          document.querySelector("main, [role='main']") || document.body;
      }
      const vis = (e) => e.getClientRects().length > 0;
      // By code point, not UTF-16 unit: half an emoji can fail the JSON insert.
      const clip = (s, n) => Array.from((s || "").replace(/\s+/g, " ").replace(/\*/g, "").trim()).slice(0, n).join("");
      const push = (arr, s, n, max) => { s = clip(s, n); if (s && arr.length < max && !arr.includes(s)) arr.push(s); };
      const mask = maskPii;
      const textOf = (id) => { const n = id && document.getElementById(id); return n ? n.textContent : ""; };
      // A radio/checkbox's own label is the OPTION ("Yes"); the question is the group's.
      const nameOf = (el) => {
        if (el.matches('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"]')) {
          const g = el.closest("fieldset, [role='radiogroup'], [role='group']");
          const q = g && (g.querySelector("legend")?.textContent || g.getAttribute("aria-label") ||
            textOf((g.getAttribute("aria-labelledby") || "").split(/\s+/)[0]));
          if (q && q.trim()) return q;
        }
        return getFieldLabel(el) || el.getAttribute("role") || el.tagName.toLowerCase();
      };

      const alerts = [];
      // Alerts page-wide on a page-level form: toasts are portaled to the end of <body>.
      for (const el of (pageLevel ? document : scope).querySelectorAll('[role="alert"], [aria-live="assertive"]')) {
        if (vis(el)) push(alerts, mask(el.textContent), 120, 5);
      }
      // Polite regions only inside the form, minus counters ("519 / 1500", "page 1 of 2").
      const COUNTER_RE = /^(\d[\d\s,./]*|page\s*\d+\s*of\s*\d+.*)$/i;
      for (const el of scope.querySelectorAll('[aria-live="polite"]')) {
        const t = clip(el.textContent, 200);
        if (vis(el) && t && !COUNTER_RE.test(t)) push(alerts, mask(t), 120, 5);
      }
      const invalid = [];
      for (const el of scope.querySelectorAll('[aria-invalid="true"]')) {
        if (!vis(el)) continue;
        push(invalid, nameOf(el), 80, 8);
        for (const attr of ["aria-errormessage", "aria-describedby"]) {
          for (const id of (el.getAttribute(attr) || "").split(/\s+/).filter(Boolean)) {
            const msg = document.getElementById(id);
            if (msg && vis(msg)) push(alerts, mask(msg.textContent), 120, 5);
          }
        }
      }
      // Required-and-empty, ARIA widgets included: collectUnfilledRequired() keeps only
      // native controls (its labels become user-facing questions), so a DIV combobox or
      // radiogroup Indeed marks aria-required never shows up there.
      const reqEmpty = [];
      for (const el of scope.querySelectorAll('[required], [aria-required="true"]')) {
        if (!vis(el)) continue;
        const role = el.getAttribute("role") || "";
        let empty;
        if (el.matches('input[type="radio"], input[type="checkbox"]')) {
          empty = el.name ? !document.querySelector(`input[name="${CSS.escape(el.name)}"]:checked`) : !el.checked;
        } else if (el.matches("input, select, textarea")) {
          if (el.type === "hidden" || el.type === "file") continue;
          empty = !(el.value || "").trim();
        } else if (role === "radiogroup" || role === "group") {
          empty = !el.querySelector('[aria-checked="true"], :checked');
        } else if (role === "checkbox" || role === "switch" || role === "radio") {
          empty = el.getAttribute("aria-checked") !== "true";
        } else if (role === "combobox" || role === "listbox") {
          empty = !el.querySelector('[aria-selected="true"]') &&
            /^(|select.*|choose.*|please.*|--.*)$/i.test(clip(el.textContent, 40));
        } else continue;
        if (empty) push(reqEmpty, `${nameOf(el)}${role ? ` (${role})` : ""}`, 80, 8);
      }
      // Nothing machine-readable: keep the page's own warning text and headings. Indeed's
      // structured-data-review shows "missing info" on resume cards, not on inputs.
      const notes = [];
      if (!alerts.length && !invalid.length && !reqEmpty.length) {
        for (const el of scope.querySelectorAll('[class*="error" i], [class*="warning" i], [class*="missing" i]')) {
          if (vis(el) && el.children.length <= 3) push(notes, mask(el.textContent), 80, 5);
        }
        for (const h of scope.querySelectorAll("h1, h2")) if (vis(h)) push(notes, mask(h.textContent), 60, 8);
      }
      return {
        path: clip(location.host + location.pathname, 160),
        alerts, invalid, reqEmpty, notes,
        btn: btn ? { label: clip(label, 40), disabled: !!(btn.disabled || btn.getAttribute("aria-disabled") === "true") } : null,
      };
    } catch (e) {
      return { error: String((e && e.message) || e).slice(0, 120) };
    }
  }

  // Indeed resume-selection: once a parsed upload has been refused on this browser
  // (indeedSdrRefusedAt, kept 14 days), use the Indeed Resume card instead of uploading.
  // `chosen` = skip the upload; `changed` = this round moved the selection. Only `changed`
  // is progress: counting an already-checked card every round kept the stall guard from
  // ever firing, so a refused step spun 20 rounds instead of handing back.
  const INDEED_SDR_TTL_MS = 14 * 24 * 3600 * 1000;
  async function preferIndeedResume(filled) {
    const none = { chosen: false, changed: false };
    if (detectPlatform() !== "indeed" || !/resume-selection/.test(location.pathname)) return none;
    const card = document.querySelector('[data-testid="resume-selection-structured-resume-radio-card-input"]');
    if (!card) return none;
    const { indeedSdrRefusedAt } = await storageGet("indeedSdrRefusedAt");
    if (!indeedSdrRefusedAt || Date.now() - indeedSdrRefusedAt > INDEED_SDR_TTL_MS) return none;
    if (card.checked) {
      // Indeed pre-checked it: no click, no progress, but the kind is still "indeed".
      await storageSet({ indeedLastResumeKind: "indeed" });
      return { chosen: true, changed: false };
    }
    await humanClick(document.querySelector('[data-testid="resume-selection-structured-resume-radio-card-label"]') || card);
    await sleep(humanDelay(600, 1200));
    // React may revert the click or re-mount the input: re-read the LIVE node, and upload
    // as before if it isn't checked.
    const live = document.querySelector('[data-testid="resume-selection-structured-resume-radio-card-input"]');
    if (!live || !live.checked) return none;
    filled.push("indeed-resume");
    await storageSet({ indeedLastResumeKind: "indeed" });
    return { chosen: true, changed: true };
  }

  // One retry per job after "Review your resume details" refused an UPLOADED file (15 of the
  // 14-day Indeed hand-backs, 10-06): preferIndeedResume only switched for the NEXT job, so
  // the first refused job on every browser was handed back. True = go back and retry with the
  // Indeed Resume now. The job is recorded BEFORE the walk (storage outlives a page load), so
  // a second refusal of the same job — or a walk that reloads into a fresh phase3 — hands back.
  // Only when THIS job uploaded a file and its resume-selection offered the Indeed Resume.
  async function claimIndeedSdrRetry(jobInfo, choice) {
    if (!choice || choice.kind !== "file" || !choice.offered) return false;
    const key = indeedJobKey(jobInfo);
    const { indeedSdrRetryJob } = await storageGet("indeedSdrRetryJob");
    if (indeedSdrRetryJob === key) return false;
    await storageSet({ indeedSdrRetryJob: key });
    return true;
  }

  function indeedJobKey(jobInfo) {
    const j = jobInfo || {};
    return `${j.url || ""}|${j.title || ""}@${j.company || ""}`;
  }

  // What THIS job used at resume-selection. The kind/offered keys are tagged with the job
  // (indeedResumeJob), so a job that never passed resume-selection here (a draft resumed
  // mid-form) reads "unknown" instead of inheriting the previous job's upload.
  async function indeedResumeChoiceFor(jobInfo) {
    const s = await storageGet(["indeedLastResumeKind", "indeedResumeOffered", "indeedResumeJob"]);
    if (s.indeedResumeJob !== indeedJobKey(jobInfo)) return { kind: undefined, offered: false };
    return { kind: s.indeedLastResumeKind, offered: !!s.indeedResumeOffered };
  }

  // SmartApply's own back control, live in the 🔘 census (10-05 23:26Z: `button:Go back`, page
  // header, outside <main>). Exact label only — never "Back to search" / a nav link.
  function findIndeedBackButton() {
    const flat = (t) => String(t || "").replace(/\s+/g, " ").trim().toLowerCase();
    for (const b of document.querySelectorAll('button, [role="button"]')) {
      if (b.disabled || b.closest('[hidden], [aria-hidden="true"]')) continue;
      if (!(b.offsetWidth || b.offsetHeight || b.getClientRects().length)) continue;
      if (/^(go )?back$/.test(flat(b.getAttribute("aria-label"))) || /^(go )?back$/.test(flat(b.textContent))) return b;
    }
    return null;
  }

  // structured-data-review → … → resume-selection by "Go back". The steps are SPA routes (one
  // content script carried STEP 1–7, resume-selection → questions → intervention →
  // supporting-info → structured-data-intro → structured-data-review, Bowtech 10-05 21:42Z),
  // so a click is a route change. Bounded: INDEED_BACK_MAX clicks, each must change the path
  // within 8 s, and it stops if the form is left. Only "Go back" is clicked — nothing that
  // could submit. False = not reached (the caller hands back as before).
  const INDEED_BACK_MAX = 8;
  async function walkBackToResumeSelection() {
    for (let i = 0; i <= INDEED_BACK_MAX; i++) {
      if (/resume-selection/.test(location.pathname)) return true;
      if (i === INDEED_BACK_MAX || !/\/indeedapply\/form\//.test(location.pathname)) return false;
      if (!(await isCampaignRunning())) return false;
      let back = findIndeedBackButton();
      for (let t = 0; !back && t < 10; t++) { await sleep(500); back = findIndeedBackButton(); }
      if (!back) return false;
      const before = location.pathname;
      await humanClick(back);
      for (let t = 0; location.pathname === before && t < 20; t++) await sleep(400);
      if (location.pathname === before) return false;
      await sleep(humanDelay(600, 1200));
    }
    return false;
  }


  // Indeed's "Review your resume details" refuses Continue with no alert, no aria-invalid and
  // no empty required input (#305 read all three as empty, 3/3 on 10-02). What it renders is
  // the parsed resume as cards, so what we keep is the page's STRUCTURE: the data-testids
  // present, the labels of its buttons/links, and short texts of anything badge-like
  // (missing / required / incomplete / error). Never card bodies: they are the person's
  // resume. Emails and phone-like numbers masked as in formBlockers.
  function structuredReviewSnapshot() {
    try {
      const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
      const flat = (t) => String(t || "").replace(/\s+/g, " ").trim();
      const root = document.querySelector("main, [role='main']") || document.body;
      // A badge is a LEAF whose own short text opens with the keyword ("Missing info",
      // "Add dates", "Needs attention") — never a container, whose text is the card.
      const BADGE = /^(missing|required|incomplete|add|needs?|error|invalid|fix|update|confirm)\b/i;
      const badges = [...new Set([...root.querySelectorAll("*")].filter((e) =>
        e.children.length === 0 && vis(e) && flat(e.textContent).length <= 30 && BADGE.test(flat(e.textContent)))
        .map((e) => `${e.getAttribute("data-testid") || e.tagName.toLowerCase()}:${maskPii(flat(e.textContent))}`))].slice(0, 15);
      // Actions by their first two words: "Edit Senior Software Engineer at Kaiser" → "Edit Senior".
      const acts = [...root.querySelectorAll("button, a, [role='button'], [role='link']")].filter(vis)
        .map((e) => flat(maskPii(flat(e.getAttribute("aria-label") || e.textContent))).split(" ").slice(0, 2).join(" ") +
          (e.disabled || e.getAttribute("aria-disabled") === "true" ? "(off)" : ""))
        .filter(Boolean).slice(0, 25);
      // Indeed's own error text on a card (10-05: `education-card-validation-error` was the
      // only sign of what the step refused). Masked, 120 chars each — the wording, not the card.
      const errs = [...root.querySelectorAll('[data-testid$="validation-error"]')].filter(vis)
        .map((e) => `${e.getAttribute("data-testid")}:${maskPii(flat(e.textContent)).slice(0, 120)}`).slice(0, 6);
      let ids = JSON.stringify([...new Set([...root.querySelectorAll("[data-testid]")].filter(vis)
        .map((e) => e.getAttribute("data-testid")))].slice(0, 60));
      if (ids.length > 900) ids = ids.slice(0, 900) + "…";
      // Badges, errors and actions first: they are the diagnosis, ids are context the slice may cut.
      return `badges=${JSON.stringify(badges)} errs=${JSON.stringify(errs)} acts=${JSON.stringify(acts)} ids=${ids}`;
    } catch (e) {
      return `sdr=? (${String((e && e.message) || e).slice(0, 80)})`;
    }
  }


  function formBlockersLine(fb) {
    if (!fb || fb.error) return `blockers=? (${fb ? fb.error : "none"})`;
    const list = (a) => JSON.stringify(a);
    return `path=${fb.path} alerts=${list(fb.alerts)} invalid=${list(fb.invalid)} reqEmpty=${list(fb.reqEmpty)}${
      fb.notes && fb.notes.length ? ` notes=${list(fb.notes)}` : ""} btn=${
      fb.btn ? `"${fb.btn.label}"${fb.btn.disabled ? "(disabled)" : ""}` : "none"}`;
  }

  function findFormButton() {
    // Prefer the modal's own buttons (try every visible dialog); fall back to the whole
    // document (Indeed SmartApply iframe / ATS pages have no dialog wrapper).
    for (const dlg of visibleApplyDialogs()) {
      const inDlg = findFormButtonIn(dlg);
      if (inDlg) return inDlg;
    }
    return findFormButtonIn(document);
  }

  function findFormButtonIn(scope) {
    // Look for form navigation/submit buttons.
    // Specific data-testid selectors come first — broad type="submit" is last
    // because skip-navigation links are also type="submit" and would be matched.
    const selectors = [
      'button[data-testid="submit-application-button"]',
      'button[data-testid="continue-button"]',
      'button[data-testid="submit-button"]',
      "button.ia-continueButton",
      "button#btn-submit",                          // Lever
      'button[class*="template-btn-submit"]',       // Lever
      'button[aria-label*="Continue"]',
      'button[aria-label*="Submit"]',
      'button[aria-label*="Review"]',
      'form button[type="submit"]',
    ];

    for (const sel of selectors) {
      const el = scope.querySelector(sel);
      if (el && isShownControl(el) && !isDeniedFormButton(el)) return el;
    }

    // Text-based fallback. <a> included: the 2026-09 rebuild renders actionable
    // "buttons" as anchors (the viewjob apply button already became one), so a
    // button-only sweep goes blind on exactly the layouts that need the fallback.
    const buttons = scope.querySelectorAll('button, a[href], [role="button"]');
    for (const btn of buttons) {
      if (!isShownControl(btn) || isDeniedFormButton(btn)) continue;
      if (FORM_ADVANCE_RE.test(btnLabel(btn))) return btn;
    }
    return null;
  }

  // offsetParent is null for an element that is ITSELF position:fixed — a pinned Submit at
  // the bottom of the screen is exactly that, and the old check skipped it as hidden.
  // Indeed's review-module ended 15 of 18 visits with "no button" (10-02..10-05) after every
  // field was filled. Same blind spot isVisibleBox() was written for on the captcha side.
  // A hypothesis until a live page confirms it: buttonCensus() below says which it was.
  // Only the labels that move a FORM forward get the fixed-element pass: a pinned "Apply for
  // this job" / "Apply now" header CTA (Lever, GH job pages) would otherwise outrank the real
  // Submit in the text fallback, which walks the DOM top-down.
  const FIXED_OK_RE = /^(submit|continue|review|next)\b/;
  function isShownControl(el) {
    if (el.offsetParent !== null) return true;
    return FIXED_OK_RE.test(btnLabel(el)) && isVisibleBox(el, 1, 1);
  }

  // What the page offered when we found nothing to press: every control, with WHY it was
  // not taken (op = offsetParent null, fx = itself or an ancestor fixed/sticky, box = has a
  // layout box, off = disabled). Labels cut to 3 words. Iframes and shadow roots counted: a
  // Submit inside either is invisible to querySelectorAll from here.
  function buttonCensus() {
    try {
      const flat = (t) => String(t || "").replace(/\s+/g, " ").trim();
      const fixedUp = (e) => {
        for (let n = e; n && n !== document.body; n = n.parentElement) {
          const pos = getComputedStyle(n).position;
          if (pos === "fixed" || pos === "sticky") return pos;
        }
        return "";
      };
      const rows = [...document.querySelectorAll('button, a[href], [role="button"], input[type="submit"]')]
        .map((e) => {
          const r = e.getBoundingClientRect();
          const label = maskPii(flat(e.getAttribute("aria-label") || e.textContent || e.value)).split(" ").slice(0, 3).join(" ");
          const flags = [
            e.offsetParent === null ? "op" : "",
            fixedUp(e) ? `fx:${fixedUp(e)}` : "",
            r.width && r.height ? "box" : "",
            e.disabled || e.getAttribute("aria-disabled") === "true" ? "off" : "",
          ].filter(Boolean).join(",");
          return `${e.getAttribute("data-testid") || e.tagName.toLowerCase()}:${label}[${flags}]`;
        })
        .filter((s) => !/^a:(skip to|\d+ new|report an)/i.test(s))
        .slice(0, 30);
      const shadows = [...document.querySelectorAll("*")].filter((e) => e.shadowRoot).length;
      // Headings say WHICH page this is when no button shows (an error / "already applied"
      // / still-loading shell) — page chrome only, never form values.
      const heads = [...document.querySelectorAll("h1, h2, [role='alert'], [role='status']")]
        .map((e) => maskPii(flat(e.textContent)).slice(0, 60)).filter(Boolean).slice(0, 5);
      return `iframes=${document.querySelectorAll("iframe").length} shadows=${shadows} heads=${JSON.stringify(heads)} btns=${JSON.stringify(rows)}`;
    } catch (e) {
      return `census=? (${String((e && e.message) || e).slice(0, 80)})`;
    }
  }

  function btnLabel(b) {
    return ((b.textContent || "") + " " + (b.getAttribute("aria-label") || ""))
      .replace(/\s+/g, " ").trim().toLowerCase();
  }

  // Buttons that look actionable but must NEVER be clicked mid-application.
  // - "Search": on ZipRecruiter the header search box is a `form button[type=submit]`,
  //   so a document-wide lookup returned IT and the click reloaded the results page out
  //   from under the open Quick Apply modal — the whole "form opened, next job 30s later"
  //   mystery of 08-12/08-14 (live-confirmed 08-15 via STEP diag: btn="Search" (page)).
  // - "Save & Exit" / "Close" / "Cancel": ZR's own escape hatches, sitting right next to
  //   the real advance button ("Save & Exit | Continue Application").
  const DENY_BTN_RE = /^(search|close|cancel|back|previous|save (&|and) exit|save for later|sign in|log in|skip)\b/;
  function isDeniedFormButton(b) {
    if (b.closest('form[role="search"]')) return true;
    return DENY_BTN_RE.test(btnLabel(b));
  }

  // Advance/submit labels across platforms. Anchored at the start so "Continue
  // Application" (ZipRecruiter's second step — the label that made the engine give up
  // with "no Continue/Submit button", live 08-15) matches, while "Continue browsing"
  // style decoys still don't get a free pass through the deny list above.
  const FORM_ADVANCE_RE = /^(continue|next|submit|review|apply|send application|finish|done)\b/;

  function isSubmitStep() {
    // Check buttons for submit-intent text. <a>/[role=button] included for the same
    // reason as findFormButtonIn: the 2026-09 DOM renders controls as anchors.
    const buttons = document.querySelectorAll('button, a[href], [role="button"]');
    for (const btn of buttons) {
      if (!isShownControl(btn)) continue;
      const text = btn.textContent?.trim().toLowerCase() || "";
      if (
        text.includes("submit application") ||
        text.includes("submit your application") ||
        text === "submit"
      ) {
        return true;
      }
    }
    // Detect "Review your application" page by progress bar at 100%
    const progressEl = document.querySelector('[aria-valuenow="100"], [value="100"][max="100"]');
    if (progressEl) return true;
    const progressText = document.querySelector(".ia-ProgressBar-complete, [class*='progressBar'] [class*='complete']");
    if (progressText) return true;
    return false;
  }

  // Decide whether the current step's primary button SUBMITS the application
  // (final step) or just advances to the next step. Button-text driven so it works
  // across platforms — Indeed's multi-step modal AND ZipRecruiter's often single-step
  // Quick Apply — even when Indeed's page-level progress heuristics don't apply.
  //
  // Bug this fixes (2026-07-10): on ZR the final "Submit"/"Apply" button wasn't
  // recognized by isSubmitStep(), so it was clicked as a "Continue" — the app
  // submitted but was never recorded (count stayed 0) and the job wasn't marked
  // applied (duplicate-apply risk).
  function classifyFormButton() {
    const btn = findFormButton();
    if (!btn) return { btn: null, submit: false, label: "" };
    const label = ((btn.textContent || "") + " " + (btn.getAttribute("aria-label") || ""))
      .replace(/\s+/g, " ").trim();
    const s = label.toLowerCase();
    // "Continue"/"Next"/"Review" always mean MORE steps — never a final submit,
    // even if the word "submit" appears elsewhere on the button.
    if (/\b(continue|next|review)\b/.test(s) && !/\bsubmit\b/.test(s)) {
      return { btn, submit: false, label };
    }
    const submitIntent =
      /\b(submit|finish|done)\b/.test(s) ||
      /send (your )?application/.test(s) ||
      s === "apply" || s === "apply now" || s === "apply for this job";
    return { btn, submit: submitIntent || isSubmitStep(), label };
  }

  function isFormVisible() {
    // Check if an Indeed Easy Apply modal/form is open.
    // IMPORTANT: keep selectors specific — [class*="ia-"] matches ia-IndeedApplyButton
    // (the "Apply with Indeed" button on search results), causing a false positive that
    // sends phase3 into a loop before the modal is actually open.
    const indicators = [
      ".ia-BasePage",        // Easy Apply modal root
      ".ia-InterviewPage",   // Multi-step apply interview page
      ".ia-Wizard",          // Apply wizard container
      'form[action*="apply"]',
      '[data-testid="apply-form"]',
    ];
    for (const sel of indicators) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return true;
    }
    return false;
  }

  function findResumeInput() {
    // Direct id/name match first (Greenhouse uses id="resume" with label "Attach",
    // Lever uses name="resume") — cheaper and more reliable than text sniffing.
    const byId = document.querySelector('input[type="file"][id*="resume" i], input[type="file"][name*="resume" i], input[type="file"][id*="cv" i]');
    if (byId) return byId;
    // File inputs for resume upload
    const inputs = document.querySelectorAll('input[type="file"]');
    for (const input of inputs) {
      const parent = input.closest("div, label, fieldset");
      const text = parent?.textContent?.toLowerCase() || "";
      if (text.includes("resume") || text.includes("cv") || text.includes("attach")) return input;
    }
    // Any file input as fallback
    if (inputs.length === 1) return inputs[0];
    return null;
  }

  // Self-reported snapshot of the apply form's structure — logged to the activity
  // feed so we can understand a platform's modal WITHOUT probing the site externally
  // (which trips anti-bot). Compact + truncated on purpose.
  function logFormDiagnostic() {
    try {
      const vis = (el) => el && el.offsetParent !== null;
      // Scope to the apply modal ONLY when it's a VISIBLE dialog that actually holds
      // form fields (Indeed/ZR render the form inside a role=dialog). Don't be fooled
      // by stray hidden dialogs — e.g. Greenhouse's intl-tel-input country dropdown is
      // a hidden [role=dialog] with no inputs, which used to zero out the whole DIAG.
      const dialog = Array.from(document.querySelectorAll('[role="dialog"]')).find(
        (d) => vis(d) && d.querySelector(FIELDISH_SELECTOR)
      );
      const scope = dialog || document;
      const textInputs = Array.from(scope.querySelectorAll('input[type="text"],input[type="email"],input[type="tel"],input:not([type])')).filter(vis).length;
      const textareas = Array.from(scope.querySelectorAll("textarea")).filter(vis).length;
      const fileInputs = scope.querySelectorAll('input[type="file"]').length;
      const btns = Array.from(scope.querySelectorAll("button")).filter(vis)
        .map((b) => (b.textContent || b.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim())
        .filter(Boolean).slice(0, 8).join(" | ");
      const path = window.location.pathname + window.location.search.slice(0, 30);
      const line = `FORM DIAG [${detectPlatform()}] dialog=${!!dialog} inputs=${textInputs} textarea=${textareas} file=${fileInputs} btns=[${btns}] path=${path}`;
      log(line, "");
      logBackend(line, "info"); // durable on backend — survives osascript channel loss
    } catch (e) { log(`FORM DIAG error: ${e.message}`, ""); }
  }

  // Indeed SmartApply (especially the in-page variant that renders in a
  // smartapply.indeed.com iframe, starting on the "/pre" intro step) populates its fields
  // and its Continue/Submit button ASYNC — a beat or two after the "form" phase first
  // fires. If phase3 acts on that first beat it sees an empty step (FORM DIAG inputs=0, no
  // button), fills nothing, finds no button, and bails to the next job — silently dropping
  // an applyable posting (live 2026-07-28: Indeed pool jobs completed only when the form
  // happened to render fast enough). Wait for the step to actually have something to do.
  // Poll for an actionable form button (not just "the page has fields"). False when the
  // campaign stops or the time runs out.
  async function waitForFormButton(timeoutMs) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (!(await isCampaignRunning())) return false;
      if (findFormButton()) return true;
      await sleep(500);
    }
    return false;
  }

  async function waitForFormReady(timeoutMs = 10000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (!(await isCampaignRunning())) return false;
      const dlgs = visibleApplyDialogs();
      const scope = formScope();
      // Modal platforms mid-transition: dialogs are on screen but none holds fields yet
      // (ZipRecruiter briefly renders a bare [Close] shell between steps). Scanning the
      // document here would find the page BEHIND the modal and answer "ready" instantly —
      // which is exactly how a live 08-15 run declared "no Continue/Submit button" 11s
      // after a successful step and abandoned a half-filled application.
      if (dlgs.length && scope === document) {
        const btn = findFormButton();
        if (btn && dlgs.some((d) => d.contains(btn))) return true;
        await sleep(400);
        continue;
      }
      const hasField = !!(
        findFieldBySelectorsOrLabel("firstName") ||
        findFieldBySelectorsOrLabel("email") ||
        findResumeInput() ||
        scope.querySelector(
          'input[type="radio"], input[type="checkbox"], select, textarea, input[type="text"], input:not([type])'
        )
      );
      if (hasField || findFormButton()) return true;
      await sleep(400);
    }
    return false;
  }

  // ZipRecruiter's LAST step button is also labelled "Continue" — there is no "Submit".
  // Live 08-15: the engine clicked it, the modal closed, and phase3 logged "form abandoned
  // without submit" — while ZR's own card had already flipped to "Applied". A real
  // application was filed and never recorded (no count, no dedup → duplicate-apply risk).
  // So after every non-submit click, ask the PAGE whether the application went through.
  function jobLooksApplied() {
    // Scoped to the selected job's own pane ONLY. The old `|| document` fallback
    // matched ANY visible button starting "Applied" — including OTHER jobs' cards in
    // the same results list — and confirmed the current job off a neighbour's badge.
    // No pane on screen → answer "not applied": a missed real badge costs one
    // re-visit; a false positive records an application that never happened, forever.
    const scope = document.querySelector('[data-testid="right-pane"]');
    if (!scope) return false;
    for (const b of scope.querySelectorAll("button, [role='button']")) {
      if (b.offsetParent === null) continue;
      const t = ((b.textContent || "") + " " + (b.getAttribute("aria-label") || ""))
        .replace(/\s+/g, " ").trim().toLowerCase();
      if (/^applied\b/.test(t)) return true;
    }
    return /you'?ve applied|you have applied|application (submitted|sent|received)/i
      .test((scope.textContent || "").slice(0, 5000));
  }

  async function detectSilentSubmission(timeoutMs = 9000, baselineText) {
    const start = Date.now();
    // Pre-click page text from the caller — same contract as
    // waitForSubmissionConfirmation: phrases already on screen before the click can
    // never confirm. Snapshot now as the defensive fallback.
    const baseline = (baselineText !== undefined
      ? baselineText
      : (document.body.textContent || "")
    ).toLowerCase();
    while (Date.now() - start < timeoutMs) {
      const dlgs = visibleApplyDialogs();
      // A dialog WITH fields on screen → we're mid-flow, not done.
      if (dlgs.some((d) => d.querySelector("input, textarea, select"))) return null;
      if (dlgs.length) {
        // A field-less dialog is ZR's transient bare [Close]-only shell between steps
        // — still mid-flow. Scanning past it used to read the page BEHIND the modal
        // and "confirm" text that was always there. The one signal accepted here is
        // the dialog ITSELF announcing success; otherwise wait for it to re-mount
        // with fields (→ mid-flow) or close (→ page checks below).
        const dlgText = dlgs.map((d) => d.textContent || "").join(" ").toLowerCase();
        const phrase = SUCCESS_TEXTS.find((p) => dlgText.includes(p) && !baseline.includes(p));
        if (phrase) return `text:${phrase.slice(0, 30)}`;
        await sleep(500);
        continue;
      }
      // Dialog-less mid-flow (Indeed SmartApply renders steps as full PAGES, no
      // [role=dialog]): the apply form is still on screen, so the page is not a
      // confirmation — don't scan it for success wording.
      if (isFormVisible()) return null;
      if (jobLooksApplied()) return "applied-badge";
      const conf = await waitForSubmissionConfirmation(1200, { baselineText: baseline });
      if (conf.verified) return conf.signal;
      await sleep(500);
    }
    return null;
  }

  // Record a submission that the platform accepted (called from both the explicit
  // submit step and the ZR "last Continue" path) — bookkeeping in ONE place so the two
  // paths can never drift: dedup keys, local count, and the backend application row.
  async function recordSubmittedApplication(jobInfo, coverLetter, signal) {
    await addAppliedUrl(jobInfo.url || window.location.href);
    await addAppliedJobKey(jobInfo.title, jobInfo.company);
    await recordLocalApplication(detectPlatform());
    logBackend(`✅ Applied: ${jobInfo.title} @ ${jobInfo.company}`, "ok");
    await sendMsg({
      type: "APPLICATION_SAVED",
      data: {
        job_title: jobInfo.title || "",
        company: jobInfo.company || "",
        platform: detectPlatform(),
        job_url: jobInfo.url || window.location.href,
        cover_letter: coverLetter,
        status: "applied",
        verified: true,
        verify_signal: signal,
      },
    });
  }

  async function phase3_fillForm() {
    // Any throw in here used to vanish: phase2 calls this on-stack, nothing above catches,
    // and an unhandled rejection leaves NO log line at all. From the outside that is
    // indistinguishable from "nothing happened" — live 08-15 a one-tap ZipRecruiter apply
    // died exactly this way, twice, with the last line being "form detected".
    try {
      return await _phase3_fillForm();
    } catch (e) {
      logBackend(`💥 Form filler crashed: ${e && e.message} @ ${(e && e.stack || "").split("\n")[1] || "?"}`, "error");
      await skipToNextJob();
    }
  }

  async function _phase3_fillForm() {
    if (!(await isCampaignRunning())) return;

    // Let the step finish rendering before we fill/decide (see waitForFormReady above).
    await waitForFormReady(10000);
    _aiAnswersUsed = 0; _aiBudgetNotified = false; // fresh AI budget per form

    log("Application form detected — filling fields...", "");
    logBackend(`📋 Application form detected — filling fields (${platformLabel()})`, "info");
    logFormDiagnostic();

    // Get profile and cover letter
    const storageData = await storageGet([
      "profile",
      "generatedCoverLetter",
      "coverLetterFor",
      "currentJobInfo",
    ]);
    const profile = storageData.profile || {};
    const jobInfo = storageData.currentJobInfo || {};
    // Empty until a field asks for it (see the cover-letter step below). It used to be
    // pre-loaded from storage, which is how a letter nobody typed still reached the
    // application row — and History showed it as part of what the employer received.
    let coverLetter = storageData.coverLetterFor === coverLetterKeyFor(jobInfo)
      ? (storageData.generatedCoverLetter || "")
      : "";

    let formStepCount = 0;
    const STEP_BUDGET = 20; // Safety: don't loop forever (some jobs have 10+ steps)
    let maxSteps = STEP_BUDGET; // raised once by the Indeed resume retry (its own budget)
    let stoppedEarly = false; // a break below, not the step budget, ended the loop
    // Stall guard: a step that fills nothing AND leaves the form byte-identical means
    // "Continue" is being refused (unanswered validation) — clicking it again just
    // repeats the refusal. Live 08-15: ZR's screener step ate 20 identical rounds and
    // 4 minutes before the loop gave up. Two no-progress rounds is proof enough.
    let lastSig = "";
    let stallRounds = 0;
    // Per-step wall clock, printed on the STEP line. Form time is a product metric now
    // (target: a form under 90s), and the only honest place to measure it is the live
    // run — reading it out of the activity log beats re-deriving it from timestamps.
    let prevStepAt = Date.now();
    const formSignature = () => {
      const sc = formScope();
      const q = sc === document ? document.body : sc;
      const fields = Array.from(q.querySelectorAll("input, textarea, select"))
        .map((el) => `${el.name || el.id || el.type}:${el.type === "checkbox" || el.type === "radio" ? el.checked : (el.value || "").length}`)
        .join(",");
      return `${location.pathname}|${fields}`;
    };

    while (formStepCount < maxSteps) {
      if (!(await isCampaignRunning())) {
        log("Campaign stopped — aborting form fill", "");
        // Durable: a run that dies mid-application is the single most confusing failure
        // (08-15: the form just sat there, half filled, with nothing in the log). Say
        // which job and which step, so the cause can be matched to the stop line above it.
        logBackend(`⏹ Campaign stopped mid-form (step ${formStepCount}) — ${jobInfo.title || "this job"} @ ${jobInfo.company || "?"} left unfinished`, "warn");
        return;
      }

      formStepCount++;
      // Between-step pauses are the cheapest seconds in the whole engine to give back:
      // the human-plausible part of a step is the typing and the mouse path (both kept
      // intact below), not a flat think-pause on top of them. Measured 09-06 on Igor's
      // live run: 17-31s per Indeed step, 160s for an 8-step form — target is <90s.
      await sleep(humanDelay(700, 1400));

      // Fill whatever fields are visible on this step
      let filledAny = false;
      const filled = []; // durable step summary (popup log() is lost when the popup is closed)

      // Name / email / phone. Only a field we actually TYPED INTO counts as progress —
      // see fillIdentityFields for the bug that rule closes.
      const profileGaps = [];
      if (await fillIdentityFields(profile, filled, profileGaps)) filledAny = true;

      // Cover letter — written on demand, only because this step showed a field for it.
      const clEl = findFieldBySelectorsOrLabel("coverLetter");
      if (clEl && !(clEl.value || "").trim()) {
        const _letter = await ensureCoverLetter();
        if (_letter) {
          quickSet(clEl, _letter);
          coverLetter = _letter;
          await sleep(humanDelay(1200, 2200));
          filledAny = true; filled.push("cover");
        }
      }

      // Indeed: a freshly uploaded PDF makes Indeed parse it and insert "Review your resume
      // details" (structured-data-review), which refused Continue with no visible error on
      // every upload of the 10-02 run (3/3; 14 of 17 blind hand-backs before it). Once that
      // has happened on this browser, prefer the Indeed Resume the user already has there —
      // no parse, no review step. The upload path stays for users without one.
      const indeedResume = await preferIndeedResume(filled);
      if (indeedResume.changed) filledAny = true;
      const indeedResumeChosen = indeedResume.chosen;

      // Resume upload
      const resumeInput = indeedResumeChosen ? null : findResumeInput();
      if (indeedResumeChosen) await storageSet({ indeedResumeJob: indeedJobKey(jobInfo) });
      if (resumeInput && !resumeInput.files?.length) {
        try {
          await uploadResume(resumeInput);
          await sleep(humanDelay(1200, 2200));
          filledAny = true; filled.push("resume");
          // indeedResumeOffered: whether this resume-selection ALSO offered the Indeed Resume —
          // a structured-data-review refusal is retried with it only when there is one.
          if (detectPlatform() === "indeed") await storageSet({ indeedLastResumeKind: "file",
            indeedResumeOffered: !!document.querySelector('[data-testid="resume-selection-structured-resume-radio-card-input"]'),
            indeedResumeJob: indeedJobKey(jobInfo) });
        } catch (e) {
          log("Resume upload failed: " + e.message, "err");
        }
      }

      // Screener radio questions (Yes/No and multi-choice).
      // Strategy: pick "Yes" for unanswered Yes/No groups (covers 18+, eligibility,
      // background-check acknowledgements). For non-Yes/No groups pick first option.
      const radiosFilled = await fillRadioQuestions();
      if (radiosFilled > 0) {
        await sleep(humanDelay(500, 1000));
        filledAny = true; filled.push(`radio×${radiosFilled}`);
      }

      // Required attestation/consent checkboxes (self-attestation, "I certify…").
      const checkboxesFilled = await fillCheckboxes();
      if (checkboxesFilled > 0) {
        await sleep(humanDelay(300, 700));
        filledAny = true; filled.push(`checkbox×${checkboxesFilled}`);
      }

      // Dropdown screener questions (salary, "how did you hear", demographic/EEO,
      // and custom required <select>s). Without this the form stalls on
      // "Choose an option to continue." Deterministic where possible, AI for the rest.
      const selectsFilled = await fillSelectQuestions();
      if (selectsFilled > 0) {
        await sleep(humanDelay(400, 800));
        filledAny = true; filled.push(`select×${selectsFilled}`);
      }

      // Custom (DIV-based) dropdowns — Indeed's demographic/screener comboboxes.
      const combosFilled = await fillComboboxes();
      if (combosFilled > 0) {
        await sleep(humanDelay(400, 800));
        filledAny = true; filled.push(`combo×${combosFilled}`);
      }

      const textsFilled = await fillTextQuestions();
      if (textsFilled > 0) {
        await sleep(humanDelay(300, 700));
        filledAny = true; filled.push(`text×${textsFilled}`);
      }

      if (filledAny) {
        log(`Form step ${formStepCount}: filled fields`, "ok");
      }

      // No-progress detection (see stall guard above) — evaluated BEFORE the click so a
      // refused step ends in an honest hand-back instead of a silent 20-round spin.
      {
        const sig = formSignature();
        if (!filledAny && sig === lastSig) stallRounds++;
        else stallRounds = 0;
        lastSig = sig;
        if (stallRounds >= 2) {
          // Whole line capped: POST /activity drops a message over 2000 chars (422).
          logBackend(Array.from(`🖐 ${dialogSnapshot()} ${formBlockersLine(formBlockers())}`).slice(0, 1950).join(""), "warn");
          if (/structured-data-review/.test(location.pathname)) {
            const choice = await indeedResumeChoiceFor(jobInfo);
            const indeedLastResumeKind = choice.kind; // this job's, else unknown
            logBackend(Array.from(`🧾 sdr resume=${indeedLastResumeKind || "?"} ${structuredReviewSnapshot()}`).slice(0, 1950).join(""), "warn");
            // Stamped only by a refusal of an UPLOADED file: if the Indeed Resume is refused
            // too, re-stamping would keep the tailored PDF off Indeed forever for nothing,
            // and the 14 days must be allowed to run out.
            if (indeedLastResumeKind !== "indeed") await storageSet({ indeedSdrRefusedAt: Date.now() });
            // Don't hand THIS job back yet: the stamp above makes resume-selection pick the
            // Indeed Resume, so walk back there and go through again — once per job.
            if (await claimIndeedSdrRetry(jobInfo, choice)) {
              logBackend(`↩️ Indeed refused the uploaded resume at "Review your resume details" — retrying with your Indeed Resume: ${jobInfo.title || "this job"} @ ${jobInfo.company || "?"}`, "info");
              if (await walkBackToResumeSelection()) {
                lastSig = ""; stallRounds = 0; prevStepAt = Date.now();
                // Its own step budget: the retry walks the form again. Still bounded — one retry per job.
                maxSteps = formStepCount + STEP_BUDGET;
                continue;
              }
              logBackend(`↩️ Couldn't get back to the resume step (@${location.pathname.slice(-70)}) — handing the job back`, "warn");
            }
          }
          // The invariant: submitted-complete-and-honest OR handed back with a reason.
          // handBackJob is the right channel (records the reason + unfilled labels and
          // advances the walk) — NOT DETECTION_TRIPPED, which means "a human check is
          // blocking us" and pauses the whole campaign behind a captcha CTA.
          await handBackJob(
            `the form step wouldn't accept our answers — "${classifyFormButton().label || "Continue"}" refused ${stallRounds + 1}× with nothing left to fill${
              profileGaps.length ? ` — your profile has nothing for: ${[...new Set(profileGaps)].join(", ")}` : ""
            }`,
            { title: jobInfo.title, company: jobInfo.company, platform: detectPlatform(), url: jobInfo.url,
              // The refusing screen is not a completed step — count the ones before it.
              steps: Math.max(0, formStepCount - 1) });
          await skipToNextJob();
          return;
        }
      }

      // Classify the step's primary button (submit vs continue) by its own text —
      // works on ZipRecruiter's single-step Quick Apply, not just Indeed's modal.
      const action = classifyFormButton();
      if (action.label) log(`Step ${formStepCount} button: "${action.label}" → ${action.submit ? "SUBMIT" : "continue"}`, "");
      // Durable per-step trace (backend activity log). Three ZR runs (08-12/08-14) opened the
      // Quick Apply modal, then silently ended up on the next job ~30s later — every decision
      // in between was popup-only log(), so nobody could tell WHY. Never again.
      {
        const dlgs = visibleApplyDialogs();
        const where = !action.btn ? "none" : (dlgs.some((d) => d.contains(action.btn)) ? "dialog" : "page");
        const dt = ((Date.now() - prevStepAt) / 1000).toFixed(1);
        prevStepAt = Date.now();
        // The path: a hand-back row names only the page it died on, not the page each step was on.
        logBackend(Array.from(`STEP ${formStepCount} [${platformLabel()}] Δ${dt}s @${location.pathname.slice(-70)} filled=[${filled.join(",")}]${profileGaps.length ? ` gaps=[${profileGaps.join(",")}]` : ""} btn="${action.label || "-"}" (${where}) → ${action.btn ? (action.submit ? "SUBMIT" : "continue") : "no button"} ${dialogSnapshot()}`).slice(0, 1950).join(""), "info");
      }

      // Check if this is the final submit step
      if (action.submit) {
        // Review mode — fill everything but don't submit; report what's filled.
        const reviewMode = (await storageGet("reviewMode")).reviewMode === true;
        if (reviewMode) {
          const nameEl = findFieldBySelectorsOrLabel("firstName") || findFieldBySelectorsOrLabel("fullName");
          const emailEl = findFieldBySelectorsOrLabel("email");
          const resumeEl = findResumeInput();
          const summary = `name="${nameEl?.value || ""}" email="${emailEl?.value || ""}" resume=${resumeEl?.files?.length ? resumeEl.files[0].name : "NONE"} radios=${document.querySelectorAll('input[type="radio"]:checked').length}`;
          log(`REVIEW MODE — filled, awaiting your tap: ${summary}`, "ok");
          // Publish to the dashboard review card and wait for the human's verdict.
          const choice = await awaitReview({
            id: jobInfo.url || `${jobInfo.title}@${jobInfo.company}`,
            job_title: jobInfo.title,
            company: jobInfo.company,
            description: (jobInfo.description || "").slice(0, 800),
            cover_letter: "",
            summary,
            job_url: jobInfo.url || "",
          });
          if (choice !== "submit") {
            logBackend(`⏭️ Skipped by you: ${jobInfo.title} @ ${jobInfo.company}`, "info");
            // A user skip must still advance the walk (mirrors phase_ats): a bare
            // return left the form open with the campaign "running" — detectPhase()
            // keeps answering "form", the phase observer only fires on CHANGE, so
            // nothing ever moved again.
            await skipToNextJob();
            return;
          }
          logBackend(`👍 You approved — submitting: ${jobInfo.title} @ ${jobInfo.company}`, "ok");
        }
        log("Final step — submitting application...", "");
        // Last-look pause is longer than mid-form steps — real users
        // re-read the summary before committing.
        await sleep(humanDelay(3000, 8000));
        // Re-check AFTER the pause: a Stop during the last-look pause must not
        // result in a submitted application. This is the one click we can never
        // take back, so guard it tightest.
        if (!(await isCampaignRunning())) {
          log("Campaign stopped — not submitting application", "");
          return;
        }
        // FAIL CLOSED (ROADMAP_E2E.md P1): if a resume file input is visibly present on
        // the submit step but empty (upload 401'd), don't send a resume-less application.
        // Conservative on purpose — native ZR/Indeed usually pre-attach the resume from the
        // account (no file input, or a filename chip), so this never blocks the happy path.
        {
          const rz = findResumeInput();
          if (rz && !rz.files?.length && !document.body.textContent.includes("resume.pdf")) {
            logBackend(`⏭️ Skipped (no resume attached): ${jobInfo.title} @ ${jobInfo.company} — not submitting a resume-less application`, "error");
            // Honest outcome = hand back with a reason + advance (same channel as the
            // stall guard above; phase_ats does the same on its resume guard). A bare
            // return here dead-stopped a "running" campaign on the open form.
            await handBackJob(
              "resume didn't attach (required) — not submitting a resume-less application",
              { title: jobInfo.title, company: jobInfo.company, platform: detectPlatform(), url: jobInfo.url,
                // The blocked submit screen is not a completed step.
                steps: Math.max(0, formStepCount - 1) });
            await skipToNextJob();
            return;
          }
        }
        const submitBtn = findFormButton();
        if (submitBtn) {
          // Mark applied + count BEFORE the click: submitting can navigate the whole
          // page (ZipRecruiter returns to results), which would kill this context
          // before an after-the-fact write runs — leaving the job un-marked and
          // re-appliable, and the count unincremented. Recording first is nav-safe.
          await addAppliedUrl(jobInfo.url || window.location.href);
          await addAppliedJobKey(jobInfo.title, jobInfo.company);
          await recordLocalApplication(detectPlatform());

          // Pre-click snapshot: success wording already on this page must not verify.
          const baselineText = document.body.textContent || "";
          if (shouldMisclick()) await performMisclick(submitBtn);
          await humanClick(submitBtn);

          // Wait for a real signal the platform accepted the submission.
          // Without this, every Submit click was counted as 'applied' —
          // captcha, error toasts, or silent failures all looked the same.
          const result = await waitForSubmissionConfirmation(45000, { baselineText });

          const currentPlatform = detectPlatform();
          if (result.verified) {
            log(`Applied (verified ${result.signal}): ${jobInfo.title} @ ${jobInfo.company}`, "ok");
            logBackend(`✅ Applied: ${jobInfo.title} @ ${jobInfo.company}`, "ok");
            await sendMsg({
              type: "APPLICATION_SAVED",
              data: {
                job_title: jobInfo.title || "",
                company: jobInfo.company || "",
                platform: currentPlatform,
                job_url: jobInfo.url || window.location.href,
                cover_letter: coverLetter,
                status: "applied",
                verified: true,
                verify_signal: result.signal,
              },
            });
          } else {
            // Submit clicked but confirmation not detected within the window. Most of
            // these are false negatives (slow confirmation) — save as applied but
            // flagged unconfirmed so the count reflects reality without silently
            // over- or under-counting. Still do NOT re-apply (added above).
            log(`Submit unconfirmed for ${jobInfo.title} @ ${jobInfo.company} (${result.signal})`, "warn");
            logBackend(`⚠️ Applied (unconfirmed): ${jobInfo.title} @ ${jobInfo.company}`, "warn");
            await sendMsg({
              type: "APPLICATION_SAVED",
              data: {
                job_title: jobInfo.title || "",
                company: jobInfo.company || "",
                platform: currentPlatform,
                job_url: jobInfo.url || window.location.href,
                cover_letter: coverLetter,
                status: "applied_unconfirmed",
                verified: false,
                verify_signal: result.signal,
              },
            });
          }

          // Either way, navigate away from this job
          await sleep(humanDelay(4000, 6000));
          await goBackToJobList();
          return;
        } else {
          log("Submit button not found on final step", "err");
          logBackend(`⚠️ Submit button vanished on final step — giving up on ${jobInfo.title} @ ${jobInfo.company}`, "warn");
          stoppedEarly = true;
          break;
        }
      }

      // Click Continue/Next button to proceed to next step
      const navBtn = action.btn || findFormButton();
      if (navBtn) {
        log(`Clicking "${(navBtn.textContent || "").trim()}"...`, "");
        await sleep(humanDelay(1000, 2000));
        // Re-check after the pause so a Stop mid-step halts before advancing.
        if (!(await isCampaignRunning())) {
          log("Campaign stopped — aborting before next step", "");
          return;
        }
        const sigBefore = formSignature();
        // Pre-click snapshot for the silent-submit check below: text that was on the
        // page BEFORE this Continue can never count as its confirmation.
        const baselineText = document.body.textContent || "";
        await humanClick(navBtn);
        // Wait for the NEXT step to actually render (signature change) rather than a flat
        // sleep — ZipRecruiter re-mounts its modal, and acting on the old beat made phase3
        // read an empty page and abandon the application.
        {
          const t0 = Date.now();
          while (Date.now() - t0 < 12000) {
            await sleep(500);
            if (!(await isCampaignRunning())) return;
            if (formSignature() !== sigBefore) break;
          }
        }
        await sleep(humanDelay(800, 1500));

        // That "Continue" may have BEEN the submit (ZipRecruiter has no Submit button).
        // Sizing this window is the single biggest lever on form time: it is PURE waiting.
        // On modal platforms detectSilentSubmission bails on its first beat (a dialog with
        // fields = mid-flow), but Indeed's SmartApply renders steps as PAGES — no dialog —
        // so every mid-form Continue sat out the full window. Measured 09-06 on the live
        // run: ~9s of a 17-31s step, eight steps deep (160s for one form).
        // When the next step is already showing its own advance button we are mid-flow, so
        // a short window is enough to still catch the one shape that looks the same — a
        // post-apply page that also carries a "Continue…" button. With no button on screen
        // (the shape a real silent submit leaves behind) we keep the full window.
        const nextStepShowing = !!classifyFormButton().btn;
        const silent = await detectSilentSubmission(nextStepShowing ? 2500 : 9000, baselineText);
        if (silent) {
          log(`Applied (verified ${silent}): ${jobInfo.title} @ ${jobInfo.company}`, "ok");
          await recordSubmittedApplication(jobInfo, coverLetter, silent);
          await sleep(humanDelay(2000, 4000));
          await goBackToJobList();
          return;
        }
      } else {
        // No button yet — the step may still be rendering (Smart-Apply renders async, and
        // late steps race the same way the first one does). Wait for the BUTTON itself:
        // waitForFormReady() answers "ready" on any input in the shell, so on Indeed's
        // review-module it returned in ~1 s and 9 of 17 visits were abandoned (10-05,
        // FORM DIAG → "abandoned" one second apart) while Submit rendered 2–8 s in on the
        // visits that went through — the same saved draft was dropped in one run and
        // submitted in the next.
        const waitedAt = Date.now();
        if (await waitForFormButton(15000)) {
          logBackend(`⏳ button appeared after ${((Date.now() - waitedAt) / 1000).toFixed(1)}s @${location.pathname.slice(-70)}`, "info");
          continue;
        }
        logBackend(`⚠️ Form step had no Continue/Submit button (${location.hostname}) — giving up on this job — ${dialogSnapshot()}`, "warn");
        logBackend(Array.from(`🔘 @${location.pathname.slice(-70)} ${buttonCensus()}`).slice(0, 1950).join(""), "warn");
        stoppedEarly = true;
        break;
      }
    }

    if (formStepCount >= maxSteps && !stoppedEarly) {
      log("Too many form steps — skipping job", "err");
      logBackend(`⚠️ Too many form steps (${maxSteps}) — giving up on ${jobInfo.title} @ ${jobInfo.company}`, "warn");
      // A hand-back with a reason, not a silent skip — the same channel the stall guard in
      // this loop already uses on every phase3 board (Indeed and ZipRecruiter alike).
      await handBackJob(`the form ran past ${maxSteps} steps without reaching Submit`,
        { title: jobInfo.title, company: jobInfo.company, url: jobInfo.url, platform: detectPlatform(), steps: formStepCount });
      await skipToNextJob();
      return;
    }

    // If we got here without submitting, skip to next job
    logBackend(`⏭️ Form abandoned without submit: ${jobInfo.title} @ ${jobInfo.company} (${formStepCount} step${formStepCount === 1 ? "" : "s"})`, "warn");
    await skipToNextJob();
  }

  // Hand the backend the posting text we just read off the page. The server can never
  // fetch an Indeed page (403), so without this the job row keeps the search card's
  // snippet — "From $40,000 a yearFull-time" — and the resume tailor, the fit judge and
  // the interview kit all read that as if it were the job.
  //
  // Called where we COMMIT to applying (right after currentJobInfo is set, past the fit
  // decision), not from uploadResume(): that only runs when the form happens to show a
  // file input, which Indeed smartapply often doesn't — so the one platform this exists
  // for would have been the one platform it never fired on. Here it also lands well
  // before the resume fetch, which is what tailors against the row.
  //
  // Best-effort and awaited briefly; a describe failure must never cost an application.
  //
  // Company and location ride along: the server fills them into the row only where it
  // has none (save_description), so the page heals a card that came up empty.
  async function recordJobDescription(title, company, description, url, location = "") {
    if (!url || (description || "").length < 300) return;
    try {
      await Promise.race([
        sendMsg({
          type: "SAVE_JOB_DESCRIPTION",
          data: { title, company, location, description, url, platform: detectPlatform() },
        }),
        sleep(8000),
      ]);
    } catch {}
  }

  async function uploadResume(fileInput) {
    // Resume now lives in Supabase Storage (Phase 3.5). Backend returns a
    // signed URL valid for 1h that the content script fetches directly —
    // the Storage URL doesn't need our Bearer token, the signature is the
    // capability.
    const { currentJobInfo } = await storageGet("currentJobInfo");
    // Signed-URL fetch is transiently flaky ("No resume on server" x2 then success,
    // live 2026-08-08) — retry with backoff instead of failing the whole application
    // on a storage/token blip. 3 tries covers the observed transient window.
    let signed = null, lastErr = "";
    for (let i = 0; i < 3 && !signed?.url; i++) {
      if (i > 0) await sleep(2000 * i);
      signed = await sendMsg({ type: "GET_RESUME_URL", jobUrl: currentJobInfo?.url });
      if (!signed?.url) lastErr = signed?.error || "No resume on server";
    }
    if (!signed?.url) throw new Error(lastErr);

    let res = await fetch(signed.url);
    if (!res.ok) { await sleep(2000); res = await fetch(signed.url); } // one retry on download too
    if (!res.ok) throw new Error(`Resume download failed: ${res.status}`);

    const blob = await res.blob();
    const file = new File([blob], "resume.pdf", { type: "application/pdf" });

    // Create a DataTransfer to set the file input
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;

    // Trigger events
    fileInput.dispatchEvent(new Event("change", { bubbles: true }));
    fileInput.dispatchEvent(new Event("input", { bubbles: true }));

    log("Resume uploaded to form", "ok");
  }

  // =========================================================================
  // Navigation helpers
  // =========================================================================

  async function skipToNextJob() {
    if (!(await isCampaignRunning())) return;

    // Tap swipe-pool (all-platforms): the background walks the approved queue, so a
    // skip/fail on a native board must advance the POOL, not walk the Indeed search
    // list (which would apply un-swiped jobs). Pool-gated → auto mode is unaffected.
    if ((await storageGet("atsPlatform")).atsPlatform === "pool") {
      await sendMsg({ type: "ATS_JOB_DONE" });
      return;
    }

    const data = await storageGet(["pendingJobs", "currentJobIndex"]);
    const jobs = data.pendingJobs || [];
    const idx = (data.currentJobIndex || 0) + 1;

    if (idx >= jobs.length) {
      // All pending jobs processed — go back to list for next page
      log("All jobs on this page processed", "");
      await goBackToJobList();
      return;
    }

    await storageSet({ currentJobIndex: idx });
    const nextJob = jobs[idx];
    log(`Next job (${idx + 1}/${jobs.length}): ${nextJob.title}`, "");

    await sleep(humanDelay(3000, 5000));
    if (!(await isCampaignRunning())) return; // the check above is 3-5 s old by now

    const platform = detectPlatform();
    let targetUrl;
    if (platform === "indeed") {
      // Use /viewjob?jk= directly — card hrefs (/rc/clk?...) may redirect back to
      // /jobs?...&vjk= which detectPhase() now treats as "list", re-running phase1.
      targetUrl = nextJob.jk
        ? `https://www.indeed.com/viewjob?jk=${nextJob.jk}`
        : nextJob.url;
    } else {
      targetUrl = nextJob.url;
    }
    window.location.href = targetUrl;
  }

  // ── keyword rotation ──────────────────────────────────────────────────────
  // Multiple keywords are DISTINCT searches, not one mashed query. Cramming them
  // ("healthcare marketing social media manager project manager ai engineer") returns
  // junk the fit-gate then skips wholesale. We search ONE phrase at a time.
  //
  // BREADTH, NOT DEPTH (09-19). The walk used to give keyword #1 pagesPerKeyword() pages
  // back to back before it ever touched keyword #2 — and the platform cap (15 applications
  // per board per day) ran out INSIDE that first keyword. Live measure over 84 applications:
  // 39 of 39 on "Welder", zero on "Fabrication" and "Fitter", with 221 Indeed rows in that
  // user's pool. Depth is strictly worse here for two reasons:
  //   1. we ask Indeed for sort=date, so page N is the postings that sit N×10 positions
  //      OLDER — page 4 of "Welder" is a stale tail, page 1 of "Fitter" is today's head;
  //   2. the cap is spent before the tail phrases are searched AT ALL, run after run.
  // So the walk takes ONE page per keyword, rotates through the whole list, then starts a
  // second lap (page 2 of each). The page budget is unchanged — only the ORDER is. The
  // cap is sliced the same way (keywordSubCap), because rotating at page boundaries alone
  // is not enough: a single results page can hold more Easy Apply cards than the whole
  // daily cap, so phrase #1 would still walk out with all of it.
  //
  // Which phrase LEADS a run is the SERVER's call (modules/keyword_rotation — cursor in
  // campaign_states.filters, applied to the list the dashboard arms us with). The
  // extension always starts at index 0 of the list it is handed and keeps NO cursor of
  // its own across runs; two cursors would advance independently and drift.
  async function keywordList() {
    const d = await storageGet("campaignFilters");
    return (d.campaignFilters?.keywords || []).filter(Boolean);
  }

  // Pages per keyword for the whole run (~24 pages total, at least 4 each) — the same
  // budget as the depth era, now spent one page per LAP instead of back to back. A single
  // keyword therefore still walks 24 pages deep, exactly as before.
  async function pagesPerKeyword() {
    const n = (await keywordList()).length || 1;
    return Math.max(4, Math.floor(24 / n));
  }

  // The board's daily cap, sliced across the keywords. Floor of 1 so a long list still
  // applies to something per phrase; one keyword means no slicing at all.
  async function keywordSubCap() {
    const n = (await keywordList()).length;
    if (n <= 1) return MAX_APPLICATIONS_PER_PLATFORM;
    return Math.max(1, Math.floor(MAX_APPLICATIONS_PER_PLATFORM / n));
  }

  // Applications filed TODAY per search phrase, per platform — the sub-cap's ledger,
  // written by recordLocalApplication.
  //
  // The ledger carries its OWN day and is keyed by the PHRASE (2026-10-06). It used to
  // borrow `todayDate` and key by list index, and both were wrong:
  //   1. background.js rolls the day over in three places (onInstalled — which every
  //      reload fires —, onStartup, Start) by resetting todayCount + platformCounts and
  //      stamping todayDate = today. None of them knew about keywordCounts, so yesterday's
  //      ledger survived under today's date. Live 10-06: 0 Indeed applications that day,
  //      yet every phrase read "spent" → "Per-keyword cap reached" on the first posting,
  //      "Indeed exhausted (all keywords searched)" 2 minutes into a 15-minute run.
  //   2. The server rotates WHICH phrase leads a run (kw_cursor), so index 0 is a
  //      different phrase from one run to the next — the slice was charged to the wrong one.
  // A ledger that dates itself (ledgerOf) cannot be resurrected by any other writer of todayDate.
  function ledgerOf(k) {
    if (!k || typeof k !== "object" || k.day !== localDay()) return { day: localDay() };
    return k;
  }

  function keywordKey(phrase) {
    return String(phrase || "").trim().toLowerCase();
  }

  async function getKeywordCounts(platform) {
    return ledgerOf((await storageGet("keywordCounts")).keywordCounts)[platform] || {};
  }

  // The phrase the live search is on ("" with no keywords at all), from a storage snapshot
  // holding campaignFilters + kwIndex — same clamping as currentKeywordIndex.
  function keywordKeyOf(s) {
    const kws = (s.campaignFilters?.keywords || []).filter(Boolean);
    if (!kws.length) return "";
    return keywordKey(kws[Math.min(Math.max(s.kwIndex || 0, 0), kws.length - 1)]);
  }

  async function currentKeywordKey() {
    return keywordKeyOf(await storageGet(["campaignFilters", "kwIndex"]));
  }

  async function currentKeywordIndex() {
    const d = await storageGet(["campaignFilters", "kwIndex"]);
    const kws = (d.campaignFilters?.keywords || []).filter(Boolean);
    if (!kws.length) return 0;
    return Math.min(Math.max(d.kwIndex || 0, 0), kws.length - 1);
  }

  // True when this phrase has spent its slice of the board cap. The walk rotates instead
  // of applying — that is the whole point of the slice.
  async function keywordCapReached(platform) {
    const kws = await keywordList();
    if (kws.length <= 1) return false;
    // A pool / ATS queue walk has no search phrase (recordLocalApplication skips the
    // ledger for it too), and rotating there would steer the queue walk into a board
    // search — the 09-13 failure in reverse. The slice governs the live search only.
    if ((await storageGet("atsPlatform")).atsPlatform) return false;
    const counts = await getKeywordCounts(platform);
    return (counts[await currentKeywordKey()] || 0) >= (await keywordSubCap());
  }

  // A phrase whose search came back with no results at all: deeper laps of it would be
  // empty too, so retire it instead of paying a page load per lap to re-learn that.
  // Per RUN (background clears kwDone at start), not per day.
  async function retireKeyword() {
    const i = await currentKeywordIndex();
    const d = await storageGet("kwDone");
    const done = d.kwDone || [];
    if (done.includes(i)) return;
    await storageSet({ kwDone: [...done, i] });
  }

  async function currentSearchPhrase() {
    const d = await storageGet(["campaignFilters", "kwIndex"]);
    const kws = (d.campaignFilters?.keywords || []).filter(Boolean);
    const ws = d.campaignFilters?.work_setting === "hybrid" ? "hybrid" : "";
    if (!kws.length) return ws;
    const i = Math.min(Math.max(d.kwIndex || 0, 0), kws.length - 1);
    return [kws[i], ws].filter(Boolean).join(" ");
  }

  // Rotate to the next phrase that still has something to search, wrapping into the next
  // lap (= the next page of every phrase). Skips phrases that are retired or have spent
  // their slice of the cap. Returns false when nothing is left on this board — the caller
  // turns that into PLATFORM_EXHAUSTED.
  async function advanceKeyword(platform) {
    const kws = await keywordList();
    if (!kws.length) return false;
    const laps = await pagesPerKeyword();
    const cap = await keywordSubCap();
    const counts = await getKeywordCounts(platform);
    const st = await storageGet(["kwIndex", "kwLap", "kwDone"]);
    const done = new Set(st.kwDone || []);
    let i = Math.min(Math.max(st.kwIndex || 0, 0), kws.length - 1);
    let lap = Math.max(0, st.kwLap || 0);
    // At most one full pass: starting anywhere in the list, n steps wrap exactly once.
    for (let step = 0; step < kws.length; step++) {
      i += 1;
      if (i >= kws.length) { i = 0; lap += 1; }
      if (lap >= laps) return false; // page budget spent for every phrase
      if (done.has(i)) continue;
      if (kws.length > 1 && (counts[keywordKey(kws[i])] || 0) >= cap) continue;
      await storageSet({ kwIndex: i, kwLap: lap });
      log(`Keyword done — switching to "${kws[i]}" (page ${lap + 1})`, "");
      logBackend(`Next keyword: ${kws[i]} (page ${lap + 1})`, "info");
      return true;
    }
    return false; // every phrase is retired or has spent its slice of the cap
  }

  // Every list nav rotates now — one page per keyword, then the next lap. "stop" means
  // the page budget or the per-keyword caps are spent on this board.
  async function pageOrRotate(platform) {
    return (await advanceKeyword(platform)) ? "rotated" : "stop";
  }

  async function goBackToJobList() {
    if (!(await isCampaignRunning())) return;

    // Tap swipe-pool: an apply here already emitted APPLICATION_SAVED, which advances
    // the pool queue in the background. Don't ALSO navigate to the board search — that
    // would fight the pool walk for the automation tab. Pool-gated → auto unaffected.
    if ((await storageGet("atsPlatform")).atsPlatform === "pool") return;

    const platform = detectPlatform();
    if (platform === "ziprecruiter") return await goBackToZipRecruiterJobList();
    return await goBackToIndeedJobList();
  }

  async function goBackToIndeedJobList() {
    const count = await getPlatformCount("indeed");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`Indeed daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}) — trying another platform.`, "ok");
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "indeed", reason: "platform daily cap" });
      return;
    }

    // Same keyword's next page, next keyword's page 1, or all-done.
    const decision = await pageOrRotate("indeed");
    if (decision === "stop") {
      log("All keywords searched here — switching platform or finishing.", "ok");
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: detectPlatform(), reason: "all keywords searched" });
      return;
    }

    const data = await storageGet("campaignFilters");
    const filters = data.campaignFilters || {};

    const params = new URLSearchParams();
    // ONE keyword per search (rotated), plus the hybrid query-token when set.
    const q = await currentSearchPhrase();
    if (q) params.set("q", q);
    const locMap = { usa: "United States", remote: "remote", europe: "" };
    const loc = locMap[filters.location] !== undefined ? locMap[filters.location] : (filters.location || "");
    if (loc) params.set("l", loc);
    // Keep the geo radius across pagination (miles) — else page 2+ silently widens the
    // search back to the whole location. Omitted for remote / no radius. Matches
    // background.js buildIndeedUrl's radiusMilesFor guard.
    const radN = Number(filters.search_radius_miles);
    if (loc && String(loc).toLowerCase() !== "remote" && Number.isFinite(radN) && radN > 0) {
      params.set("radius", String(Math.round(radN)));
    }
    if (filters.job_type) {
      const jtMap = { "full-time": "fulltime", "part-time": "parttime", contract: "contract" };
      if (jtMap[filters.job_type]) params.set("jt", jtMap[filters.job_type]);
    }
    params.set("iafilter", "1");
    params.set("sort", "date");

    // Indeed paginates via start= (10/page). The breadth walk rotated the keyword just
    // above, so the page number is the LAP, not a per-keyword page counter: lap 0 is
    // page 1 of every phrase, lap 1 is page 2 of every phrase, and so on.
    const lapI = Math.max(0, (await storageGet("kwLap")).kwLap || 0);
    params.set("start", String(lapI * 10));

    const url = `https://www.indeed.com/jobs?${params.toString()}`;
    log("Returning to job list...", "");
    // 15-30 s between pages — rapid page-flipping triggers Cloudflare rate limiting
    await sleep(humanDelay(15000, 30000));
    if (!(await isCampaignRunning())) return;
    window.location.href = url;
  }

  async function goBackToZipRecruiterJobList() {
    const count = await getPlatformCount("ziprecruiter");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`ZipRecruiter daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}) — trying another platform.`, "ok");
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "ziprecruiter", reason: "platform daily cap" });
      return;
    }

    // Same keyword's next page, next keyword's page 1, or all-done.
    const decision = await pageOrRotate("ziprecruiter");
    if (decision === "stop") {
      log("All keywords searched here — switching platform or finishing.", "ok");
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: detectPlatform(), reason: "all keywords searched" });
      return;
    }

    const data = await storageGet("campaignFilters");
    const filters = data.campaignFilters || {};

    const params = new URLSearchParams();
    const q = await currentSearchPhrase();
    if (q) params.set("search", q);
    const locMap = { usa: "United States", remote: "Remote", europe: "" };
    const loc = locMap[filters.location] !== undefined ? locMap[filters.location] : (filters.location || "");
    if (loc) params.set("location", loc);
    if (filters.job_type) {
      const jtMap = { "full-time": "full_time", "part-time": "part_time", contract: "contract" };
      if (jtMap[filters.job_type]) params.set("employment_type[]", jtMap[filters.job_type]);
    }

    // ZipRecruiter paginates via `page` (20 jobs a page). Same rule as Indeed above: the
    // keyword just rotated, so the page number comes from the lap.
    const page = Math.max(0, (await storageGet("kwLap")).kwLap || 0) + 1;
    if (page > 1) params.set("page", String(page));

    const url = `https://www.ziprecruiter.com/jobs-search?${params.toString()}`;
    log("Returning to ZipRecruiter job list...", "");
    await sleep(humanDelay(10000, 20000));
    if (!(await isCampaignRunning())) return;
    window.location.href = url;
  }

  // Recover when the campaign window lands on a ZipRecruiter page that isn't
  // list/detail/form (e.g. /jobseeker/home after an apply, or a session redirect).
  // Without this the phase is "unknown" forever and the campaign silently stalls.
  // Loop-guarded: if ZR keeps bouncing us off the search, stop with a clear message.
  async function recoverZipRecruiterPhase() {
    const st = await storageGet("zrRecoveries");
    const n = (st.zrRecoveries || 0) + 1;
    if (n > 4) {
      log("ZipRecruiter kept redirecting away from search — switching platform", "err");
      logBackend("ZipRecruiter redirect loop — switching platform", "warn");
      await storageSet({ zrRecoveries: 0 });
      await sendMsg({ type: "PLATFORM_EXHAUSTED", platform: "ziprecruiter", reason: "redirect loop" });
      return;
    }
    await storageSet({ zrRecoveries: n });

    const data = await storageGet("campaignFilters");
    const filters = data.campaignFilters || {};
    const params = new URLSearchParams();
    // Recovery nav — keep searching the CURRENT keyword (no rotation here).
    const q = await currentSearchPhrase();
    if (q) params.set("search", q);
    const locMap = { usa: "United States", remote: "Remote", europe: "" };
    const loc = locMap[filters.location] !== undefined ? locMap[filters.location] : (filters.location || "");
    if (loc) params.set("location", loc);
    if (filters.job_type) {
      const jtMap = { "full-time": "full_time", "part-time": "part_time", contract: "contract" };
      if (jtMap[filters.job_type]) params.set("employment_type[]", jtMap[filters.job_type]);
    }
    const url = `https://www.ziprecruiter.com/jobs-search?${params.toString()}`;
    log(`Off-track on ZipRecruiter (${window.location.pathname}) — recovering to search (attempt ${n})...`, "");
    await sleep(humanDelay(3000, 6000));
    if (!(await isCampaignRunning())) return;
    window.location.href = url;
  }

  // =========================================================================
  // GREENHOUSE — external ATS single-page apply
  //
  // The biggest lever in the roadmap: most boards' "external apply" jobs funnel
  // into a handful of ATS providers. One Greenhouse recipe covers thousands of
  // companies. Standard hosted form (job-boards.greenhouse.io / boards.greenhouse.io):
  //   #first_name #last_name #email #phone (tel) #resume (file, label "Attach"),
  //   #question_* custom fields, submit button "Submit application". One page.
  //
  // Reuses the universal filler helpers (findFieldBySelectorsOrLabel, screener
  // answerers, resume upload, classifyFormButton) — no board-specific navigation.
  // =========================================================================
  // Which verdict decided — the log must say whether the score was reused from the list
  // the server built (no second model call) or judged just now on this page. Only the
  // server's own word counts: a cap skip or an older backend carries no verdict_source,
  // and then the line says nothing rather than guess.
  function fitSourceNote(fit) {
    if (!fit || !fit.verdict_source) return "";
    if (fit.verdict_source === "queue") {
      const day = typeof fit.judged_at === "string" ? fit.judged_at.slice(5, 10) : "";
      return ` · score from your list${day ? ` (judged ${day})` : ""}, not re-judged`;
    }
    return ` · judged now${fit.fresh_because ? ` (${fit.fresh_because})` : ""}`;
  }

  async function phase_ats(platform) {
    if (!(await isCampaignRunning())) return;
    const label = platform === "lever" ? "Lever" : platform === "ashby" ? "Ashby" : "Greenhouse";

    const count = await getPlatformCount(platform);
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`${label} daily limit reached. Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }
    _aiAnswersUsed = 0; _aiBudgetNotified = false; // fresh AI budget per form

    // Job title: Greenhouse h1 = title; Lever h1 = company, title in .posting-headline h2
    let jobTitle = "";
    if (platform === "lever") {
      jobTitle = (document.querySelector('.posting-headline h2, [class*="posting-headline"] h2')?.textContent || document.title.split(" - ")[1] || "").replace(/\s+/g, " ").trim();
    }
    if (!jobTitle) jobTitle = (document.querySelector("h1")?.textContent || "").replace(/\s+/g, " ").trim();

    let jobCompany = "";
    const cm = window.location.pathname.match(/^\/(?:embed\/[^\/]+|([^\/]+))/);
    if (cm && cm[1]) jobCompany = cm[1].replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    const descEl = document.querySelector('.job__description, .posting-page, #content, [class*="description" i], main');
    // Keep more of the posting — the tap card shows this as the primary thing to read.
    const jobDesc = (descEl?.textContent || "").replace(/\s+/g, " ").trim().slice(0, 3000);
    const jobUrl = window.location.href.split("?")[0];

    if (!jobTitle) {
      logBackend(`⏭️ ${label}: no job title on page — skipping to next`, "warn");
      await sendMsg({ type: "ATS_JOB_DONE" }); // advance the pool walk past this page
      return;
    }

    // Never re-apply (URL or title|company)
    const applied = await getAppliedUrls();
    const appliedJobs = await getAppliedJobKeys();
    if (applied.has(jobUrl) || appliedJobs.has(jobDedupKey(jobTitle, jobCompany))) {
      logBackend(`⏭️ Already applied: ${jobTitle} @ ${jobCompany} — next job`, "info");
      await sendMsg({ type: "ATS_JOB_DONE" }); // was a silent dead-stop: nothing advanced the queue
      return;
    }

    log(`${label} job: ${jobTitle} @ ${jobCompany}`, "");
    // Narrate the real steps to the dashboard's Live Activity — the user watches
    // that line to understand what the bot is doing right now.
    logBackend(`🔍 Reading job posting: ${jobTitle} @ ${jobCompany} — checking fit`, "info");

    // Fit gate (M1). In the TAP swipe-pool the user already approved this job by swiping
    // (atsPlatform="pool"), so we DON'T re-run the AI fit check — just apply it. AUTO mode
    // runs the gate and fails closed. (reviewMode is always off now — the swipe is the
    // review — so we key off the pool marker, not reviewMode.)
    const preApproved = (await storageGet("atsPlatform")).atsPlatform === "pool";
    if (preApproved) {
      logBackend(`Applying your approved pick: ${jobTitle} @ ${jobCompany}`, "info");
    } else {
      // job_url lets the background match this page to the head of the server queue and
      // send its row id: the server then reuses the verdict the queue was built from
      // instead of judging the posting a second time on the page text (10-02: 3 of 5
      // opened postings lost to that second verdict). The full href: jobUrl drops the
      // query, and an employer-hosted Greenhouse page carries its posting id in ?gh_jid=.
      const fit = await sendMsg({ type: "ASSESS_FIT", data: { job_title: jobTitle, company: jobCompany, description: jobDesc, job_url: window.location.href } });
      if (!(await isCampaignRunning())) return; // Stop landed while the judge answered
      const src = fitSourceNote(fit);
      // FAIL CLOSED: only proceed on an explicit "apply" (null/missing verdict → skip).
      if (!fit || fit.decision !== "apply") {
        const why = (fit && fit.reason ? fit.reason : "fit check unavailable — skipped for safety").slice(0, 140);
        logBackend(`Skipped (fit ${(fit && fit.fit_score != null) ? fit.fit_score : "?"}): ${jobTitle} @ ${jobCompany} — ${why}${src}`, (!fit || fit.failClosed) ? "warn" : "info");
        await sendMsg({ type: "ATS_JOB_DONE" }); // fit-skip must still advance the pool walk
        return;
      }
      if (fit.judged) logBackend(`Good fit (${fit.fit_score}): ${jobTitle} @ ${jobCompany}${src}`, "info");
    }

    await recordJobDescription(jobTitle, jobCompany, jobDesc, jobUrl);

    // The letter is NOT written here any more — it is written if and when this form asks
    // for one (fillCoverLetterIfAsked below). Writing it upfront cost a model call on
    // every ATS application and, since nothing ever typed it into the form, bought
    // nothing but a row in History claiming the employer had read it.
    await storageSet({
      currentJobInfo: { title: jobTitle, company: jobCompany, description: jobDesc, url: jobUrl },
    });
    let coverLetter = "";

    const profile = (await storageGet("profile")).profile || {};

    log(`${label} — filling application...`, "");
    logBackend(`📋 Filling ${label} application: ${jobTitle} @ ${jobCompany}`, "info");
    logFormDiagnostic();

    const fillField = async (name, val) => {
      const el = findFieldBySelectorsOrLabel(name);
      if (el && !(el.value || "").trim() && val) { await typeValue(el, val); await sleep(humanDelay(1500, 3000)); return true; }
      return false;
    };
    // Greenhouse splits first/last; Lever uses a single "name" field. Try both —
    // fill the single full-name field only if the split fields aren't present.
    const filledFirst = await fillField("firstName", profile.name || "");
    await fillField("lastName", profile.last_name || "");
    if (!filledFirst) {
      const fullName = [profile.name, profile.last_name].filter(Boolean).join(" ");
      await fillField("fullName", fullName);
    }
    await fillField("email", await resolveEmail(profile));
    await fillField("phone", profile.phone || "");

    // Resume: the file input can render slightly after the text fields — retry a few
    // times before giving up. The outcome is logged DURABLY (logBackend) because the
    // 50-line activity ring buffer floods during a ZR campaign and used to swallow this
    // line — making it impossible to tell "attached" from "No resume on server".
    let resumeInput = findResumeInput();
    for (let i = 0; i < 6 && !resumeInput; i++) { await sleep(1000); resumeInput = findResumeInput(); }
    // Track attach outcome so we can FAIL CLOSED before submit: never send an ATS
    // application with a required-but-empty resume (a resume-less app silently torches
    // the user's reputation — see ROADMAP_E2E.md P1).
    const resumeRequired = !!resumeInput;
    let resumeOk = false;
    if (resumeInput) {
      if (!resumeInput.files?.length) {
        try {
          await uploadResume(resumeInput);
          // GH/Ashby accept the file ASYNC and swap the <input> for a "resume.pdf" chip.
          // In a throttled background window that can take 10-30s — far longer than a fixed
          // sleep. POLL for the reflected state instead of checking once (2026-08-04: the old
          // single 2-4s check false-negatived and fail-closed EVERY GH submit -> todayCount 0).
          // Live-verified: DataTransfer+change DOES attach; the chip/filename just appears late.
          resumeOk = false;
          for (let i = 0; i < 15 && !resumeOk; i++) {
            await sleep(2000);
            resumeOk = !!findResumeInput()?.files?.length || document.body.textContent.includes("resume.pdf");
          }
          logBackend(`${label} resume: ${resumeOk ? "attached ✓" : "NOT reflected after 30s"}`, resumeOk ? "info" : "error");
        }
        catch (e) { logBackend(`${label} resume upload FAILED: ${e.message}`, "error"); }
      } else {
        resumeOk = true;
        logBackend(`${label} resume: already attached`, "info");
      }
    } else {
      logBackend(`${label} resume: file input not found`, "error");
    }

    // Cover letter — AFTER the resume, BEFORE the screener answers. Greenhouse hides the
    // textarea behind an "Enter manually" chooser, so this reveals it first; on Lever the
    // field is open and fills directly. If the form has no such field (Indeed's wizard
    // never shows one) nothing is written and nothing is charged.
    // 6s ceiling: the resume step above already polls up to 30s for hydration, so this is
    // the tail case, not the common one — and it is only ever paid when no field is found.
    coverLetter = await fillCoverLetterIfAsked(label, 6000);

    // Screener questions — reuse the generic answerers (Loop 4 core).
    // These are the quietest 100 seconds in the product: each text answer is an AI
    // round-trip and every filler carries a human delay, and none of them logged a
    // thing. Live 09-21 a healthy Greenhouse apply sat silent for 102s between
    // "resume: attached ✓" and the submit — one second under the E2E driver's stall
    // threshold, and indistinguishable from a dead run to anyone watching. Say what is
    // happening: silence that means "working" has to look different from silence that
    // means "stuck".
    const screener = {
      radio: await fillRadioQuestions(),
      text: await fillTextQuestions(),
      select: await fillSelectQuestions(),
      combo: await fillComboboxes(),
    };
    const answered = Object.entries(screener)
      .filter(([, n]) => typeof n === "number" && n > 0)
      .map(([k, n]) => `${k}×${n}`)
      .join(" ");
    logBackend(answered ? `${label}: answered ${answered}` : `${label}: no screener questions`, "info");
    // The screener filler can reach a cover-letter field this step missed (a label our
    // chooser search didn't recognise). It writes through ensureCoverLetter, so storage
    // is the one place that knows whether a letter was actually produced for this job —
    // read it back rather than recording "" over a letter the employer received.
    if (!coverLetter) {
      const _st = await storageGet(["generatedCoverLetter", "coverLetterFor"]);
      if (_st.coverLetterFor === coverLetterKeyFor({ url: jobUrl, title: jobTitle })) {
        coverLetter = _st.generatedCoverLetter || "";
      }
    }
    await sleep(humanDelay(1500, 2500));

    if (!(await isCampaignRunning())) return;

    const action = classifyFormButton();
    if (action.label) log(`${label} button: "${action.label}" → ${action.submit ? "SUBMIT" : "continue?"}`, "");
    let submitBtn = findFormButton();
    if (!submitBtn) {
      // React/async ATS forms (Ashby, some Greenhouse) render the submit button LATE —
      // and a throttled background window makes hydration take 30-90s. Poll before giving
      // up so we don't declare "not found" on a form that just hadn't finished rendering
      // (2026-08-04 root-cause: forms bailed before hydrating → todayCount stayed 0).
      logBackend(`${label}: waiting for the submit button to render (up to 40s)`, "info");
      for (let i = 0; i < 8 && !submitBtn; i++) {
        await waitForFormReady(5000);
        submitBtn = findFormButton();
      }
    }
    if (!submitBtn) {
      await handBackJob("no submit button after ~40s (form may not have finished rendering)", { title: jobTitle, company: jobCompany, platform });
      return;
    }

    // Review mode (semi-auto / human-reviews-before-submit): fill everything but do
    // NOT click submit. Report exactly what got filled so the user (or an E2E test)
    // can confirm the form is correct before sending. Nothing is recorded/applied.
    const reviewMode = (await storageGet("reviewMode")).reviewMode === true;
    if (reviewMode) {
      const nameEl = findFieldBySelectorsOrLabel("firstName") || findFieldBySelectorsOrLabel("fullName");
      const emailEl = findFieldBySelectorsOrLabel("email");
      const phoneEl = findFieldBySelectorsOrLabel("phone");
      const resumeEl = findResumeInput();
      // A successful attach can REMOVE the file input (Greenhouse swaps #resume for a
      // filename chip once it accepts the DataTransfer set) — so a null input does NOT
      // mean "no resume". Treat the uploaded filename appearing on the page as attached.
      const resumeStatus = resumeEl?.files?.length
        ? resumeEl.files[0].name
        : (document.body.textContent.includes("resume.pdf") ? "attached (chip)" : "NONE");
      const filledTextareas = Array.from(document.querySelectorAll("textarea")).filter((t) => (t.value || "").trim()).length;
      const checkedRadios = document.querySelectorAll('input[type="radio"]:checked').length;
      const summary = `name="${(nameEl?.value || "").slice(0, 40)}" email="${emailEl?.value || ""}" phone="${phoneEl?.value || ""}" resume=${resumeStatus} screener-textareas=${filledTextareas} radios=${checkedRadios} submitBtn="${(submitBtn.textContent || "").replace(/\s+/g, " ").trim().slice(0, 30)}"`;
      log(`REVIEW MODE — filled, awaiting your tap: ${summary}`, "ok");
      // Publish the filled application to the dashboard review card and wait for the
      // human's verdict. Only "submit" falls through to the real submit path below.
      const choice = await awaitReview({
        id: jobUrl,
        job_title: jobTitle,
        company: jobCompany,
        description: jobDesc || "",
        cover_letter: coverLetter || "",
        summary,
        job_url: jobUrl,
      });
      if (choice !== "submit") {
        logBackend(`⏭️ Skipped by you: ${jobTitle} @ ${jobCompany}`, "info");
        await sendMsg({ type: "ATS_JOB_DONE" }); // tap-skip advances to the next card
        return; // never submits, never records applied
      }
      logBackend(`👍 You approved — submitting: ${jobTitle} @ ${jobCompany}`, "ok");
    }

    // FAIL CLOSED: never submit an application whose resume field is required but empty.
    // A silent resume-less submission is irreversible and reputationally harmful; skip
    // and log so it surfaces (usually a resume-upload 401 — the auth path, see P1/P2).
    if (resumeRequired && !resumeOk) {
      await handBackJob("resume didn't attach (required) — not submitting a resume-less application", { title: jobTitle, company: jobCompany, platform });
      return;
    }

    // LEVER hCaptcha — we do NOT solve captchas (compliance/ban-safety), so this job is
    // HANDED BACK. The form is already FILLED above. GH's invisible reCAPTCHA auto-solves
    // (zero-touch), so this fires ONLY on a real interactive challenge (isDetected signal =
    // hcaptcha iframe). The oldest path treated any GH/Lever captcha as "passive — continuing"
    // and fake-submitted into the unsolved hCaptcha → silent fail (Lever = 0 applies ever).
    //
    // The next one swung to NOTIFY: DETECTION_TRIPPED — the channel that means "the walk is
    // parked and a human is about to clear this wall" — plus ATS_JOB_DONE on the very next
    // line. Every part of that was wrong (audit 09-25, findings 3+5):
    //   · ATS_JOB_DONE navigates THIS tab to the next card (advanceAtsQueue → navigatePoolNext
    //     → chrome.tabs.update), so the filled form is destroyed. "We filled it — open it and
    //     press submit yourself" pointed at a page that no longer exists: a claim about state
    //     that does not survive;
    //   · DETECTION_TRIPPED persists captchaWaiting, and its clearers are the in-page pause
    //     loops / Start / Stop — none of which happen here — so the flag outlived the wall,
    //     begged the user for a posting the run had abandoned, and muted the watchdogs;
    //   · and the job itself was written off with no reason: ATS_JOB_DONE PATCHes status
    //     "skipped" silently, and only in a pool run.
    // handBackJob is the channel this path always wanted, and the one the rest of phase_ats
    // already uses for "this one can't be finished by us": it records the REASON + the unfilled
    // labels, files the durable to-do row the popup block and the dashboard rail read, flips the
    // pool job out of `approved` so it never re-queues, and advances the walk — the intentional
    // advance (#72) survives, only the pause claim goes. It writes no hand-off flag, so nothing
    // here can mute a watchdog or leave a banner standing over a posting we left behind. One
    // channel on purpose: the second wording (the old 🧩 logBackend line) repeated the same
    // "заполнено, открой и submit" promise the record had already broken.
    if (platform === "lever") {
      const _det = isDetected();
      if (/hcaptcha/i.test(_det.signal || "")) {
        await handBackJob(
          `Lever asks for a human captcha at submit (${_det.signal}) — we don't solve those, so nothing was sent. Apply by hand if you want this one`,
          { title: jobTitle, company: jobCompany, platform }
        );
        return;
      }
    }

    await sleep(humanDelay(2000, 5000));
    if (!(await isCampaignRunning())) { log("Campaign stopped — not submitting", ""); return; }

    // Record BEFORE the click — submit navigates to the thank-you page.
    await addAppliedUrl(jobUrl);
    await addAppliedJobKey(jobTitle, jobCompany);
    await recordLocalApplication(platform);
    // Submit belt: if the click reloads the page (Greenhouse → /confirmation), this
    // context dies before APPLICATION_SAVED below — whichever script wakes on the
    // confirmation page records it from this key (recordPendingSubmitOnConfirmation).
    await storageSet({
      pendingAtsSubmit: {
        url: jobUrl, jobKey: jobDedupKey(jobTitle, jobCompany),
        title: jobTitle, company: jobCompany, platform,
        letter: coverLetter || "", ts: Date.now(),
      },
    });
    // Pre-click snapshot: ATS pages carry the job description (with thank-you-ish
    // boilerplate) on the apply page itself — it must not verify the submit.
    const baselineText = document.body.textContent || "";
    if (shouldMisclick()) await performMisclick(submitBtn);
    await humanClick(submitBtn);

    const result = await waitForSubmissionConfirmation(45000, { submitBtn, baselineText });
    // VALIDATION-BLOCKED detection (council 2026-08-04, honesty fix): no confirmation AND
    // we're still on the same page with the same submit button AND required fields remain
    // empty / error text present → the form NEVER left the page. We optimistically counted
    // it before the click (nav-safe) — un-count it and hand the job back with the exact
    // unfilled fields, instead of lying "Applied (unconfirmed)".
    if (!result.verified && submitBtn.isConnected && window.location.href === jobUrl) {
      // Checked first: with every field filled, the code prompt leaves no leftover and no
      // invalid field, and the job would be counted "Applied (unconfirmed)" — unsent.
      if (greenhouseAsksEmailCode()) {
        await storageRemove("pendingAtsSubmit"); // nothing left the page — nothing to record
        await subtractLocalApplication(platform);
        await handBackJob(
          "Greenhouse asked for the verification code it just emailed you — nothing was sent. Open the posting, enter the code from your inbox and submit",
          { title: jobTitle, company: jobCompany, platform }
        );
        return;
      }
      const leftover = collectUnfilledRequired();
      // A REAL validation error = an aria-invalid field or a non-empty alert — NOT the
      // ubiquitous "* indicates a required field" legend (the old bare /required/ regex
      // matched that on every form → false "0 required unfilled" hand-backs, 2026-08-09
      // mercor). Primary signal is leftover (required fields still empty).
      const invalidEl = document.querySelector('[aria-invalid="true"], [role="alert"]:not(:empty)');
      if (leftover.length || invalidEl) {
        await storageRemove("pendingAtsSubmit"); // nothing left the page — nothing to record
        await subtractLocalApplication(platform);
        const reason = leftover.length
          ? `submit blocked — ${leftover.length} required field${leftover.length === 1 ? "" : "s"} still empty`
          : "submit blocked by a form validation error — please finish it yourself";
        await handBackJob(reason, { title: jobTitle, company: jobCompany, platform });
        return;
      }
    }
    // RECEIPT (council #3 trust primitive): freeze the confirmation-page moment —
    // screenshot + text + signal — regardless of verified/unconfirmed, so every submit
    // is auditable ("did it land?") without relying on employer emails.
    await sendMsg({
      type: "RECEIPT_CAPTURE",
      data: {
        job_title: jobTitle, company: jobCompany, platform,
        job_url: jobUrl, page_url: window.location.href,
        verified: result.verified, signal: result.signal,
        snippet: (document.body.innerText || "").replace(/\s+/g, " ").slice(0, 600),
      },
    });
    if (result.verified) {
      log(`Applied (verified ${result.signal}): ${jobTitle} @ ${jobCompany}`, "ok");
      logBackend(`✅ Applied: ${jobTitle} @ ${jobCompany}`, "ok");
    } else {
      logBackend(`⚠️ Applied (unconfirmed): ${jobTitle} @ ${jobCompany}`, "warn");
    }
    await sendMsg({
      type: "APPLICATION_SAVED",
      data: {
        job_title: jobTitle, company: jobCompany, platform,
        job_url: jobUrl, cover_letter: coverLetter,
        status: result.verified ? "applied" : "applied_unconfirmed",
        verified: result.verified, verify_signal: result.signal,
      },
    });
    // Recorded here — the belt must not write this submit a second time on a later wake.
    await markSubmitRecorded(jobUrl);
  }

  // Tap-mode review ("тапалка"): publish the filled application to the DASHBOARD via
  // chrome.storage (the same cross-window bridge the captcha hand-off uses), then wait
  // for the human's Approve/Skip. The dashboard renders a rich card (job, cover letter,
  // filled summary) and writes the verdict back to chrome.storage.reviewDecision; a small
  // in-window overlay is a fallback if the dashboard tab is closed. Resolves
  // "submit" | "skip". Fails SAFE: on timeout it SKIPS — never auto-submits unreviewed.
  async function awaitReview(review) {
    const id = review.id || review.job_url || String(Date.now());
    try {
      await storageSet({
        reviewPending: { ...review, id, at: Date.now() },
        reviewDecision: null,
      });
    } catch (_) { /* storage unavailable — overlay fallback still works */ }
    logBackend(`📝 Ready to review: ${review.job_title} @ ${review.company} — approve it on your dashboard`, "info");

    return new Promise((resolve) => {
      let done = false;
      const finish = (choice) => {
        if (done) return;
        done = true;
        clearInterval(poll);
        clearTimeout(timer);
        try { if (overlay) overlay.remove(); } catch (_) {}
        storageRemove(["reviewPending", "reviewDecision"]);
        resolve(choice);
      };

      // Poll for a decision made on the dashboard card (approve → submit, skip → skip).
      const poll = setInterval(async () => {
        try {
          const d = (await storageGet("reviewDecision")).reviewDecision;
          if (d && d.id === id) finish(d.decision === "approve" ? "submit" : "skip");
        } catch (_) { /* transient */ }
      }, 700);

      // Fail safe: never hang forever, never auto-submit — default to skip.
      const timer = setTimeout(() => {
        logBackend(`⏭️ Review timed out (30 min) — skipped: ${review.job_title} @ ${review.company}`, "warn");
        finish("skip");
      }, 30 * 60 * 1000);

      // In-window fallback overlay (dashboard card is the primary surface).
      const overlay = buildReviewOverlay(review, finish);
    });
  }

  // Small automation-window overlay — a fallback for awaitReview() when the dashboard
  // tab isn't open. Its buttons resolve the same review as the dashboard card.
  function buildReviewOverlay(review, finish) {
    const existing = document.getElementById("hd-review-card");
    if (existing) existing.remove();

    const card = document.createElement("div");
    card.id = "hd-review-card";
    card.style.cssText = [
      "position:fixed", "z-index:2147483647", "right:20px", "bottom:20px",
      "width:320px", "max-width:calc(100vw - 40px)",
      "background:#ffffff", "color:#1a1a2e",
      "border:1px solid #e2e2ea", "border-radius:14px",
      "box-shadow:0 12px 40px rgba(0,0,0,0.22)",
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
      "padding:16px", "line-height:1.4",
    ].join(";");

    const title = document.createElement("div");
    title.style.cssText = "font-weight:700;font-size:14px;margin-bottom:4px;";
    title.textContent = "✋ Tap — review & submit";

    const hint = document.createElement("div");
    hint.style.cssText = "font-size:10px;color:#9a9aa8;margin-bottom:8px;";
    hint.textContent = "Also on your HireDrop dashboard";

    const job = document.createElement("div");
    job.style.cssText = "font-size:12px;color:#4a4a5a;font-weight:600;margin-bottom:10px;";
    job.textContent = `${review.job_title || ""} @ ${review.company || ""}`;

    const body = document.createElement("div");
    body.style.cssText = "font-size:11px;color:#4a4a5a;background:#f6f6fb;border-radius:8px;padding:8px;margin-bottom:12px;word-break:break-word;max-height:110px;overflow:auto;";
    body.textContent = review.summary || "Form filled.";

    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:8px;";

    const skipBtn = document.createElement("button");
    skipBtn.textContent = "Skip";
    skipBtn.style.cssText = "flex:1;padding:9px;border-radius:9px;border:1px solid #e2e2ea;background:#fff;color:#6b6b7b;font-weight:600;font-size:13px;cursor:pointer;";

    const submitBtn = document.createElement("button");
    submitBtn.textContent = "Submit ✓";
    submitBtn.style.cssText = "flex:2;padding:9px;border-radius:9px;border:none;background:#635bff;color:#fff;font-weight:700;font-size:13px;cursor:pointer;";

    skipBtn.addEventListener("click", () => finish("skip"));
    submitBtn.addEventListener("click", () => finish("submit"));

    row.appendChild(skipBtn);
    row.appendChild(submitBtn);
    card.appendChild(title);
    card.appendChild(hint);
    card.appendChild(job);
    card.appendChild(body);
    card.appendChild(row);
    (document.body || document.documentElement).appendChild(card);
    return card;
  }

  function detectPhaseGreenhouse() {
    // The apply form is inline on the job page.
    const hasCoreField = document.querySelector('#first_name, #email, input[id="first_name"], input[type="file"]');
    const hasSubmit = Array.from(document.querySelectorAll("button, input[type=submit]"))
      .some((b) => /submit application/i.test((b.textContent || b.value || "")));
    if (hasCoreField && (hasSubmit || document.querySelector("h1"))) return "form";
    return "unknown";
  }

  function detectPhaseLever() {
    // Lever apply form lives at /{company}/{uuid}/apply with name/email/resume fields.
    if (!/\/apply\/?$/.test(window.location.pathname)) return "unknown";
    const hasCoreField = document.querySelector('input[name="name"], input[name="email"], input[type="file"][name="resume"]');
    return hasCoreField ? "form" : "unknown";
  }

  // =========================================================================
  // Phase detection & routing
  // =========================================================================

  function detectPhase() {
    const platform = detectPlatform();
    if (platform === "greenhouse") return detectPhaseGreenhouse();
    if (platform === "lever") return detectPhaseLever();
    if (platform === "ashby") return detectPhaseAshby();
    if (platform === "ziprecruiter") return detectPhaseZipRecruiter();
    if (platform === "linkedin") return detectPhaseLinkedIn();
    return detectPhaseIndeed();
  }

  // LinkedIn Easy Apply (PLATFORMS_MASTER_PLAN.md Phase 2). Type A native — applies via the
  // user's logged-in LinkedIn session. We ONLY touch Easy Apply (the in-modal 1-click subset,
  // .jobs-apply-button labelled "Easy Apply"); external-apply jobs (a "Apply" button that
  // leaves LinkedIn) are skipped. Selectors below are LinkedIn's stable public class/aria names;
  // refine live if LinkedIn ships a redesign.
  function detectPhaseLinkedIn() {
    const url = window.location.href;
    // Phase 3: the Easy Apply modal is open (artdeco dialog with the form).
    if (document.querySelector(".jobs-easy-apply-modal, .jobs-easy-apply-content, [data-test-modal] .jobs-easy-apply-form-section__grouping")) return "form";
    const modal = document.querySelector('div.artdeco-modal[role="dialog"], [aria-label*="Easy Apply" i][role="dialog"]');
    if (modal && modal.querySelector("input, select, textarea, button[aria-label*='Submit' i], button[aria-label*='next step' i]")) return "form";
    // Phase 2: a job is selected/open with an Easy Apply button (search detail pane or /jobs/view/).
    // ORDER MATTERS. On /jobs/search LinkedIn auto-selects a job whose detail-pane Easy Apply
    // button FLICKERS in and out as the SPA re-renders (live 2026-08-03: detectPhase caught
    // "detail", but by the time phase2 ran the button was gone → stuck). So treat search as
    // "list" FIRST — phase1 picks a stable Easy-Apply-badged card and navigates to its
    // canonical /jobs/view/<id> page, where the button is stable. Only THEN check for a button.
    if (url.includes("/jobs/search") || url.includes("/jobs/collections")) return "list";
    // A single-job page (/jobs/view/<id>) — phase2 opens Easy Apply on the stable button.
    if (url.includes("/jobs/view/")) return "detail";
    if (findLinkedInEasyApplyButton()) return "detail";
    return "unknown";
  }

  // The Easy Apply button only — NOT a generic "Apply" (external-apply leaves LinkedIn, which
  // we can't drive as the user). Match on the "Easy Apply" label so we never click a redirect.
  function findLinkedInEasyApplyButton() {
    const cands = Array.from(document.querySelectorAll(
      "button.jobs-apply-button, .jobs-apply-button button, button[aria-label*='Easy Apply' i]"
    ));
    for (const b of cands) {
      if (b.offsetParent === null) continue;
      const txt = `${b.getAttribute("aria-label") || ""} ${b.textContent || ""}`.toLowerCase();
      if (txt.includes("easy apply")) return b;
    }
    return null;
  }

  function linkedinModal() {
    return document.querySelector(".jobs-easy-apply-modal") ||
      document.querySelector("div.artdeco-modal[role='dialog']") ||
      document.querySelector("[role='dialog']");
  }

  // The modal's step-navigation button, by priority: Submit (final) > Review > Continue/Next.
  // LinkedIn labels these on aria-label. Returns {el, kind} or null.
  function findLinkedInNavButton() {
    const modal = linkedinModal() || document;
    const btns = Array.from(modal.querySelectorAll("button"))
      .filter((b) => b.offsetParent !== null && !b.disabled);
    const label = (b) => `${b.getAttribute("aria-label") || ""} ${b.textContent || ""}`;
    const find = (re) => btns.find((b) => re.test(label(b)));
    const submit = find(/submit application/i);
    if (submit) return { el: submit, kind: "submit" };
    const review = find(/review( your application)?/i);
    if (review) return { el: review, kind: "review" };
    const next = find(/continue to next step|next step|(^|\s)next(\s|$)/i);
    if (next) return { el: next, kind: "next" };
    return null;
  }

  // Phase 2 (LinkedIn): open the Easy Apply modal on the selected job.
  async function phase2_linkedinDetail() {
    if (!(await isCampaignRunning())) return;
    const count = await getPlatformCount("linkedin");
    if (count >= platformCap("linkedin")) {
      log(`LinkedIn daily limit reached (${count}/${platformCap("linkedin")}). Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }
    // Wait for the Easy Apply button — the detail pane loads a beat after the search results,
    // so an immediate check races (returned nothing → phase1 used to misfire). Poll ~8s.
    let btn = findLinkedInEasyApplyButton();
    for (let i = 0; i < 8 && !btn; i++) {
      await sleep(1000);
      if (!(await isCampaignRunning())) return;
      btn = findLinkedInEasyApplyButton();
    }
    if (!btn) {
      // Selected job isn't Easy Apply (external-apply) — skip it. v1 doesn't hop cards (that
      // navigation dropped the f_AL filter); the f_AL search means most selected jobs ARE
      // Easy Apply, so this is the rare case. Leave it for the user rather than misnavigate.
      logBackend("LinkedIn: selected job isn't Easy Apply — skipping (v1 applies the auto-selected Easy Apply job)", "");
      return;
    }
    log("Opening Easy Apply…", "");
    await humanClick(btn);
    await sleep(humanDelay(1500, 3000)); // modal opens → next tick detectPhase → "form"
  }

  // Phase 3 (LinkedIn Easy Apply) — v1 SEMI-AUTO: fill every step with the universal filler,
  // advance through Continue/Review, and at the FINAL Submit step DON'T click — notify the user
  // to review + submit (same "final human click" model as Lever's captcha). This ships safe
  // BEFORE live selector-verification: it can never fire a blind/wrong auto-submit. Once the
  // flow is live-verified we can enable auto-submit for zero-screener Easy Applies.
  async function phase3_linkedinForm() {
    if (!(await isCampaignRunning())) return;
    const jobTitle = (document.querySelector("h1")?.textContent || "").replace(/\s+/g, " ").trim();
    const jobCompany = (document.querySelector(
      ".job-details-jobs-unified-top-card__company-name, .jobs-unified-top-card__company-name, [class*='company-name']"
    )?.textContent || "").replace(/\s+/g, " ").trim();

    const MAX_STEPS = 8;
    for (let step = 0; step < MAX_STEPS; step++) {
      if (!(await isCampaignRunning())) return;
      if (!linkedinModal()) { log("Easy Apply modal closed", "warn"); return; }

      // Fill whatever this step shows (contact info is usually pre-filled; screeners aren't).
      const emailEl = findFieldBySelectorsOrLabel("email");
      if (emailEl && !(emailEl.value || "").trim()) await typeValue(emailEl, await resolveEmail((await storageGet("profile")).profile || {}));
      const phoneEl = findFieldBySelectorsOrLabel("phone");
      if (phoneEl && !(phoneEl.value || "").trim()) await typeValue(phoneEl, ((await storageGet("profile")).profile || {}).phone || "");
      // Full universal-filler suite (LinkedIn Easy Apply is SELECT- and combobox-heavy — live
      // recon 2026-08-01: step 1 had 2 SELECTs + 1 text input). Same set phase_ats uses.
      await fillRadioQuestions();
      await fillTextQuestions();
      await fillSelectQuestions();
      await fillComboboxes();
      await sleep(humanDelay(900, 1800));

      const nav = findLinkedInNavButton();
      if (!nav) { logBackend(`🧩 LinkedIn Easy Apply — заполнено, доделай и submit сам: ${jobTitle} @ ${jobCompany}`, "warn"); return; }

      if (nav.kind === "submit") {
        // v1: NEVER auto-submit — hand off to the human (final click keeps applications real).
        logBackend(`🧩 LinkedIn: ${jobTitle} @ ${jobCompany} — форма заполнена, ПРОВЕРЬ и нажми Submit сам (v1 semi-auto).`, "warn");
        await sendMsg({ type: "DETECTION_TRIPPED", data: { signal: "linkedin_review", url: location.href, phase: "form", job_title: jobTitle, company: jobCompany, needs_review: true } });
        return;
      }
      // Continue / Review → advance to the next step.
      await humanClick(nav.el);
      await sleep(humanDelay(1200, 2500));
    }
    logBackend("LinkedIn Easy Apply: many steps — left for you to finish", "warn");
  }

  // Phase 1 (LinkedIn): search-walk — open the next not-yet-applied Easy Apply job card.
  // Defensive/minimal for v1 (live-refine selectors); clicking a card loads it in the detail
  // pane (SPA), so the next tick runs phase2 → phase3.
  async function phase1_linkedinList() {
    if (!(await isCampaignRunning())) return;
    const count = await getPlatformCount("linkedin");
    if (count >= platformCap("linkedin")) {
      log(`LinkedIn daily limit reached (${count}/${platformCap("linkedin")}). Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }
    // The f_AL Easy-Apply URL filter is UNRELIABLE — LinkedIn rewrites the URL on load/auto-
    // select (location→f_WT, currentJobId) and drops f_AL (live 2026-08-01), so the auto-
    // selected job is often external-apply. Instead: scan the results list for cards that
    // actually show the "Easy Apply" badge, then navigate to that job's CANONICAL single-job
    // page (/jobs/view/<id>/) — a clean URL with the Easy Apply button, no filter needed.
    // Poll for the results list WITHIN this run — the MutationObserver only re-runs a phase
    // when it CHANGES, so if we returned early on an empty list (phase stays "list") phase1
    // would never fire again once cards loaded (live 2026-08-01). Wait up to ~10s here.
    let cards = [];
    for (let i = 0; i < 10; i++) {
      cards = Array.from(document.querySelectorAll(
        "li[data-occludable-job-id], .job-card-container, .scaffold-layout__list-item"
      )).filter((c) => c.offsetParent !== null);
      if (cards.length) break;
      await sleep(1000);
      if (!(await isCampaignRunning())) return;
    }
    if (!cards.length) { log("LinkedIn job list didn't load", "warn"); return; }
    const dd = await storageGet("linkedinDoneIds");
    const doneIds = new Set(dd.linkedinDoneIds || []);
    for (const c of cards) {
      if (!/easy apply/i.test(c.textContent || "")) continue; // only Easy Apply cards
      const id = c.getAttribute("data-occludable-job-id") ||
        (c.querySelector("a[href*='/jobs/view/']")?.getAttribute("href") || "").match(/\/jobs\/view\/(\d+)/)?.[1];
      if (!id || doneIds.has(id)) continue;
      // Mark done BEFORE navigating so a re-scan advances to the next Easy Apply job instead
      // of re-opening this one (v2 multi-job walk; harmless for v1's single job).
      doneIds.add(id);
      await storageSet({ linkedinDoneIds: Array.from(doneIds).slice(-200) });
      log(`Opening an Easy Apply job (${id})…`, "");
      window.location.href = `https://www.linkedin.com/jobs/view/${id}/`;
      return;
    }
    log("No new Easy Apply jobs in this list", "");
  }

  function detectPhaseAshby() {
    // Ashby guest-apply form lives at jobs.ashbyhq.com/<org>/<id>/application with
    // name/email/resume fields (React-rendered). The JD page (no /application) has an
    // "Apply for this Job" link — we navigate straight to /application (see fetch_ashby),
    // so treat a page with the core fields as the form; everything else is unknown so the
    // auto-walk's dead-job skip advances past closed/errored postings.
    const hasCoreField = document.querySelector(
      'input[name="_systemfield_name" i], input[name*="name" i], input[type="email"], input[type="file"]'
    );
    const hasApplyForm = /\/application\/?$/.test(location.pathname) || hasCoreField;
    return hasCoreField && hasApplyForm ? "form" : "unknown";
  }

  function detectPhaseIndeed() {
    const url = window.location.href;

    // Phase 3: Indeed apply form is visible (modal or full page)
    if (isFormVisible()) return "form";

    // Phase 3: Indeed's standalone Easy Apply domain (smartapply.indeed.com/beta/indeedapply/...)
    // These pages don't have the ia-* class names isFormVisible() checks for, but they ARE
    // the application form — screener questions, resume selection, review & submit pages.
    if (url.includes("smartapply.indeed.com") || url.includes("/indeedapply/")) return "form";

    // Phase 2: Standalone job detail page
    if (url.includes("/viewjob")) return "detail";

    // Phase 1: Job search results list — checked BEFORE vjk= because Indeed always
    // appends vjk= for whichever job is auto-highlighted in the right panel.
    // Treating /jobs? as "detail" would mean phase1 never runs.
    if (url.includes("/jobs?") || url.includes("/jobs#")) return "list";

    // Phase 2: vjk= outside of /jobs? context (rare direct link)
    if (url.includes("vjk=")) return "detail";

    return "unknown";
  }

  function detectPhaseZipRecruiter() {
    const url = window.location.href;

    // Phase 3: a REAL Quick Apply form (fields or a genuine apply/submit button).
    // A Close-only dialog (external-apply job) is NOT a form — don't route to phase3.
    if (findZipRecruiterApplyForm()) return "form";

    // Phase 2: any ZR surface with an lk= param AND a populated right-pane = a specific job
    // is selected. Covers /jobs-search?lk=, /candidate/search?lk=, AND /co/<Company>/Jobs?lk=
    // (clicking a search card navigates here — live 2026-07-30). The old check only matched
    // /jobs-search|/candidate/search, so a /co/…?lk= page fell through to "unknown" and the
    // walk stalled on it. Gate on the right-pane so a bare /co/ company page isn't mis-read.
    try {
      const params = new URL(url).searchParams;
      if (params.get("lk") &&
          (url.includes("/jobs-search") || url.includes("/candidate/search") ||
           document.querySelector('[data-testid="right-pane"]'))) {
        return "detail";
      }
    } catch {}

    // Phase 1: Search results (no lk= param)
    if (url.includes("/jobs-search") || url.includes("/candidate/search") ||
        /ziprecruiter\.com\/?(#.*)?$/.test(url)) return "list";

    return "unknown";
  }

  // ---------------------------------------------------------------------------
  // Retiring a human hand-off: the CLEAN PAGE is the authority, not this context
  // ---------------------------------------------------------------------------
  //
  // A full-page wall is cleared by a TOP-LEVEL NAVIGATION back to the original URL — a
  // Cloudflare managed challenge (#challenge-form / #challenge-running), Indeed's
  // "Security Check", DataDome, the challenge|security-check|blocked urls. That navigation
  // destroys this content-script context — which this file already says out loud a few
  // lines below, where it explains why cfReloadCount has to live in chrome.storage
  // ("window.location.reload() destroys this content-script context"). Both exits from the
  // captcha pause and both from the terms pause run INSIDE that dying context, so the one
  // case where the human actually SOLVED the wall was the one case where DETECTION_CLEARED
  // never fired. And nothing else retired it: content.js never touched captchaWaiting at
  // all, and in background.js only DETECTION_CLEARED / START / STOP / the 401 self-stop
  // ever write it back to null. The fresh context re-inits and the walk resumes with the
  // flag still set, so two things then run on a lie for the rest of the run (audit 09-25,
  // finding 2): the dashboard keeps telling the user to solve a wall that is already gone,
  // and nativeWalkWatchdog — which returns early while captchaWaiting is set, on purpose,
  // so it never reloads a tab under a human mid-captcha — stays muted, so a genuine freeze
  // gets no reload and no honest stop ("running" while nothing walks).
  //
  // So clearing is state-driven now: whichever context observes a page with NO wall on it
  // retires the hand-off, INCLUDING one freshly injected after the challenge navigation.
  // Three guards keep it quiet and keep a LIVE pause safe. We only speak when a hand-off is
  // actually recorded, so this is silent on every ordinary page of the walk. We speak only
  // after the page has been wall-free across a settle, so a challenge mid-render is not
  // mistaken for a solved one. And background.js accepts the report only from the tab that
  // RAISED the wall (DETECTION_TRIPPED stamps captchaWaiting.tabId), so a human still parked
  // at the wall in the automation window is never declared done because some other tab
  // happens to look clean.
  // One detector read says "no wall on this page at this millisecond" — it does NOT say
  // "the human is finished". A managed challenge repaints as it works (#challenge-form is
  // replaced by #challenge-running), an hCaptcha iframe needs a beat to lay out, and
  // isDetected() only counts boxes it can measure at ≥24x24 (see its own note), so a
  // challenge mid-render reads as clean. Retiring on that single read is the sharp edge of
  // this whole mechanism: it would pull the "your turn" banner out from under a human who
  // is still solving, and — worse for invariant 5 — un-mute nativeWalkWatchdog for the very
  // tab they are working in, so the next tick could reload their half-solved challenge.
  // So the clean page has to HOLD STILL: read, wait, read again, and only then speak.
  const WALL_CLEAR_SETTLE_MS = 5000;

  // Both walls ride ONE hand-off record, so the page is clean only when NEITHER is present:
  // a Cloudflare challenge that resolved into an Accept-Terms modal has not finished asking
  // for hands.
  function anyHumanWallPresent() {
    return isDetected().detected || detectConsentGate().gated;
  }

  async function reportCleanPageIfHandoffPending() {
    const { captchaWaiting } = await storageGet("captchaWaiting");
    if (!captchaWaiting) return false;
    if (anyHumanWallPresent()) return false;
    await sleep(WALL_CLEAR_SETTLE_MS);
    if (anyHumanWallPresent()) return false;
    // The pause may have ended some other way while we waited (Stop, the watchdog's honest
    // stop, the run moving on). Reporting about a record that is already gone would put a
    // "campaign resumed" line in the feed of a run nobody resumed.
    const after = await storageGet("captchaWaiting");
    if (!after.captchaWaiting) return false;
    const res = await sendMsg({ type: "WALL_LOOKS_CLEAR", url: window.location.href });
    return !!(res && res.cleared);
  }

  let _runPhaseActive = false;

  // LinkedIn lane is GROUNDWORK (docs/handoff/linkedin.md, linkedin-beta.js). content.js only
  // reaches a LinkedIn tab when the dev flag is on AND the user granted linkedin.com, and even
  // then the v1 phases below must not drive the page: they predate the ban-safety plan (no
  // pacing, DETECTION_TRIPPED instead of a hand-back). Flip this only in the PR that ships a
  // live-verified, tap-first LinkedIn path. The capture kit does not go through runPhase.
  const LINKEDIN_APPLY_ENABLED = false;
  let _linkedinIdleLogged = false;

  // Only the campaign's OWN tab may automate (see init for the 08-15 history). init used to
  // be the only place that asked, but runPhase has three other callers — the DOM observer
  // (any tab, any time campaignRunning is true) and CAMPAIGN_STARTED — and on 09-28 a
  // Snorkel form was filled + submitted in a tab init itself called "not the campaign tab":
  // the submit's reload then woke into init's idle gate and the application was never
  // recorded. So the answer is asked once, cached, and enforced in runPhase for every
  // caller. The cache is keyed by the stored campaignTabId because background moves the
  // campaign to the campaign window's active tab (captureActiveAutomationTab): when that
  // changes, the old answer is stale and we ask again. No answer → fail OPEN, uncached
  // (the same behaviour init always had).
  let _tabVerdict = null; // { tabId, ok, who, logged }
  async function campaignTabVerdict() {
    const { campaignTabId } = await storageGet("campaignTabId");
    if (_tabVerdict && _tabVerdict.tabId === campaignTabId) return _tabVerdict;
    let who = null;
    try { who = await sendMsg({ type: "AM_I_CAMPAIGN_TAB" }); } catch { who = null; }
    if (!who) return { ok: true, who: null };
    // `known:false` = background has no campaign tab recorded. NOT permission to take over:
    // a tab the user opened themselves would start walking the board (live 08-15).
    const ok = !(who.isCampaignTab === false || who.known === false);
    // Still idle after a re-ask (the campaign moved between two OTHER tabs): one durable
    // line per page is the signal, a line per move is noise.
    const logged = !ok && !!_tabVerdict && !_tabVerdict.ok && _tabVerdict.logged;
    _tabVerdict = { tabId: campaignTabId, ok, who, logged };
    return _tabVerdict;
  }

  async function mayAutomateThisTab() {
    const v = await campaignTabVerdict();
    if (v.ok) return true;
    if (!v.logged) {
      v.logged = true;
      log("Not the campaign tab — staying idle", "");
      // Durable: this guard silences a page completely, so when it fires by mistake
      // the run looks like it simply stopped existing — 32 minutes of a live
      // campaign with no log line at all (08-17). A decision that can end a run
      // must be visible in the same place as every other stop reason.
      logBackend(
        `🛈 Staying idle on ${location.hostname}${location.pathname.slice(0, 30)} — not the campaign tab ` +
        `(known=${v.who.known}, isCampaignTab=${v.who.isCampaignTab})`, "warn");
    }
    return false;
  }

  // …and an idle tab must notice when the campaign moves ONTO it. The verdict above is only
  // re-asked when something calls runPhase, and in an idle tab nothing does: init already
  // ran, and the observer fires only on a phase CHANGE — a wizard tab that answered "not the
  // campaign tab" a second before the capture tick adopted it sat on its form forever (live
  // 09-28, smartapply …/applybyapply, 13 min of silence). So a tab whose cached answer is
  // "not me" re-runs the gate when campaignTabId changes. It still runs ONLY if background
  // names this exact tab (AM_I_CAMPAIGN_TAB) — a tab the human opened stays idle (08-15).
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !changes.campaignTabId) return;
    if (!_tabVerdict || _tabVerdict.ok) return;
    lastPhase = "";
    runPhase();
  });

  async function runPhase() {
    if (_runPhaseActive) return;
    if (!LINKEDIN_APPLY_ENABLED && detectPlatform() === "linkedin") {
      if (!_linkedinIdleLogged) {
        _linkedinIdleLogged = true;
        log("LinkedIn apply is not enabled in this build — staying idle (capture kit only)", "");
      }
      return;
    }
    if (!(await isCampaignRunning())) return;
    if (!(await mayAutomateThisTab())) return;
    if (!navigator.onLine) {
      await waitForOnline();
      if (!(await isCampaignRunning())) return; // Stop may have landed while parked
    }
    // Two callers can pass the check at the top during the awaits above (the observer and
    // the campaignTabId listener both fire as a wizard tab is adopted) — one form, one driver.
    if (_runPhaseActive) return;
    _runPhaseActive = true;
    try {
      await _runPhaseInner();
    } finally {
      _runPhaseActive = false;
    }
  }

  async function _runPhaseInner() {
    if (!(await isCampaignRunning())) return;

    // Total daily budget (across ALL platforms) — the tier's cost/value cap. Bans are
    // counted per-platform (that rail is MAX_APPLICATIONS_PER_PLATFORM, checked in each
    // phase), but the daily budget is a cross-platform TOTAL, so it's enforced centrally
    // here — once per tick, before any apply on any platform. Without this, a user on 2+
    // platforms could submit past the budget (e.g. 20+20 > a 30/day cap): those extra
    // applications reach the employer but the backend 429s the save — invisible spend +
    // ban risk. campaignCaps.dailyTotal comes from the backend (app/db/subscriptions.py).
    {
      const c = await storageGet(["campaignCaps", "todayCount", "todayDate"]);
      const today = localDay();
      const total = c.todayDate === today ? (c.todayCount || 0) : 0;
      const dailyTotal = (c.campaignCaps && c.campaignCaps.dailyTotal > 0) ? c.campaignCaps.dailyTotal : 30;
      if (total >= dailyTotal) {
        log(`Daily budget reached (${total}/${dailyTotal}). Campaign complete.`, "ok");
        await sendMsg({ type: "STOP_CAMPAIGN" });
        return;
      }
    }

    // A dead posting is neither a challenge nor a logout — but both probes below read it
    // as one, so it has to be settled FIRST. Live 09-06: an Indeed 404 has no signed-in
    // markers, so the auth probe recorded indeed=logged_out at 22:23:24 and the dashboard
    // then refused to start a campaign on a platform Igor was perfectly signed into — a
    // dead link poisoning the platform's connection state hours later.
    if (detectPhase() === "detail" && (await bailIfDeadPosting())) return;

    // Anti-detect: a CAPTCHA / security challenge is HANDED TO THE USER — we no longer
    // auto-solve it (CapSolver dropped for compliance). The only thing we auto-handle is
    // Cloudflare's passive "Just a moment" JS interstitial, which self-resolves with no
    // user action. Everything else pauses and waits for the human to clear it.
    const det = isDetected();
    // Page is clean → reset the CF reload cap so a later genuine (transient) challenge gets
    // its full 2 retries instead of inheriting a stale count.
    if (!det.detected) {
      storageSet({ cfReloadCount: 0 }).catch(() => {});
      // …and a clean page is also the proof that a human wall got cleared. See
      // reportCleanPageIfHandoffPending(): the wall that resolves by navigation kills
      // the context that was waiting for it, so this is the only place that can tell.
      await reportCleanPageIfHandoffPending();
    }
    if (det.detected) {
      // Cloudflare JS challenge ("Just a moment") — auto-resolves in 3-5s,
      // no user action needed. Wait silently up to 15s before escalating.
      const isCfJsChallenge =
        det.signal === "title:just a moment" ||
        det.signal.includes("cdn-cgi/challenge-platform") ||
        det.signal.includes("cdn-cgi/bm");
      if (isCfJsChallenge) {
        // Durable, not popup-only: this wait (60 s, then up to two reloads) was invisible
        // in prod, so 3 minutes of "Just a moment" read as a frozen run (10-06, Indeed
        // /viewjob: three loads 64 s apart and not one line between them).
        const _cfWhere = `${location.hostname}${location.pathname.slice(0, 30)}`;
        const _cfT0 = Date.now();
        log("Cloudflare check — waiting for auto-resolve...", "");
        logBackend(`☁️ Cloudflare check on ${_cfWhere} — waiting up to 60 s for it to clear`, "info");
        for (let i = 0; i < 12; i++) {
          await sleep(5000);
          if (!isDetected().detected) {
            log("Cloudflare resolved — continuing", "ok");
            logBackend(`☁️ Cloudflare cleared after ${Math.round((Date.now() - _cfT0) / 1000)} s — continuing`, "info");
            return;
          }
        }
        // Didn't resolve in 60s — reload at most TWICE, then hand off to the human. The
        // counter lives in chrome.storage because window.location.reload() destroys this
        // content-script context: without a persistent cap a mis-detected passive signal
        // becomes an INFINITE reload loop (page reloads → fresh context → re-detect →
        // reload…), which is exactly how the pool froze on a clean /viewjob (2026-07-28).
        const _cf = await storageGet("cfReloadCount");
        const cfCount = _cf.cfReloadCount || 0;
        if (cfCount < 2) {
          await storageSet({ cfReloadCount: cfCount + 1 });
          log(`Cloudflare didn't resolve in 60s — reloading tab (try ${cfCount + 1}/2)...`, "");
          logBackend(`☁️ Cloudflare didn't clear in 60 s on ${_cfWhere} — reloading the tab (try ${cfCount + 1}/2)`, "warn");
          window.location.reload();
          await sleep(15000);
          if (!isDetected().detected) {
            await storageSet({ cfReloadCount: 0 });
            log("Cloudflare resolved after reload — continuing", "ok");
            return;
          }
        } else {
          log("Cloudflare still flagged after 2 reloads — handing off to you", "err");
          logBackend(`☁️ Cloudflare still up on ${_cfWhere} after 2 reloads — handing it to you`, "warn");
        }
        // Fall through to the human hand-off if reload also failed / retries exhausted.
      }

      // Real challenge (CF managed interstitial, reCAPTCHA / Turnstile checkbox,
      // DataDome…): hand it to the user and STAY PAUSED until they clear it. We no
      // longer force-stop after a few minutes — the user may step away and solve it
      // later; the campaign resumes the moment the page is clean. A generous 2h safety
      // cap avoids an eternal spinner if they never come back.
      // Zero-touch ATS (Greenhouse/Lever) carry a PASSIVE invisible reCAPTCHA — a badge +
      // a "protected by reCAPTCHA" notice — that Google auto-solves on submit. There is NO
      // human task. The generic detector flags that passive presence (the "…recaptcha…"
      // text / .g-recaptcha node), so on these platforms we must NOT park in the human
      // captcha-pause: that was the dead 6-7min→2h hang on GH. Log it and let the apply
      // proceed — the token is issued at submit time.
      if (["greenhouse", "lever", "ashby"].includes(detectPlatform())) {
        // GH/Ashby carry only a PASSIVE invisible reCAPTCHA (auto-solves at submit). Lever is
        // here too so phase_ats still FILLS the form — its REAL hCaptcha is caught at submit
        // (phase_ats notifies the user, #72), not at this pre-fill gate.
        logBackend(`🔓 Passive reCAPTCHA on ${detectPlatform()} — zero-touch, continuing (no human needed)`, "info");
      } else if ((await storageGet("atsPlatform")).atsPlatform === "pool") {
        // POOL (tap) mode: the automation window runs in the BACKGROUND — the user isn't
        // watching it, so a "solve the captcha in this window" hand-off is a dead end. A pool
        // job still CF-challenged after the auto-resolve + reload attempts is a dead / fake /
        // blocked posting (live 2026-07-28: a seeded fake jk `fedcba…` CF-looped the pool
        // forever). Skip it and advance to the next pick instead of parking for 2h.
        logBackend(`⏭️ Skipping (verification wall / dead posting) — moving to your next pick`, "warn");
        await storageSet({ cfReloadCount: 0 }).catch(() => {});
        await skipToNextJob();
        return;
      } else {
        log(`⚠️ CAPTCHA — pausing. Solve it in this window; the campaign resumes automatically once it's cleared.`, "err");
        await sendMsg({
          type: "DETECTION_TRIPPED",
          data: { signal: det.signal, url: window.location.href, phase: detectPhase() },
        });
        // How long we hold the whole run for one captcha. It used to be 2h, from when
        // waiting was the ONLY option — back then the alternative was killing the run.
        // It isn't any more: a captcha is account-wide WITHIN a platform, and Indeed's
        // says nothing about Greenhouse, so the run can go earn applications elsewhere
        // and come back to this board next time. Once there is somewhere to go, the
        // right wait is minutes: the campaign window is on the user's screen, so if
        // they haven't cleared it in five minutes they are not at the machine, and
        // another 115 minutes of standing still changes nothing (Igor, 09-21).
        const CAPTCHA_WAIT_MS = HUMAN_WALL_WAIT_MS;
        const _pauseStart = Date.now();
        while (Date.now() - _pauseStart < CAPTCHA_WAIT_MS) {
          await sleep(8000);
          if (!(await isCampaignRunning())) return; // user stopped it themselves
          if (!isDetected().detected) {
            log("CAPTCHA cleared — resuming campaign", "ok");
            // Tell background to drop the captchaWaiting hand-off state so the
            // popup alert and the dashboard "solve the captcha" CTA disappear.
            await sendMsg({ type: "DETECTION_CLEARED" });
            break;
          }
        }
        if (isDetected().detected) {
          const plat = detectPlatform();
          log("CAPTCHA not cleared in 5 min — moving on to another platform", "err");
          logBackend(
            `${plat} is asking for a captcha and it's still there after 5 minutes — moving on to another platform. ` +
            "Solve it any time; we'll come back to this board on the next run.",
            "warn");
          // Clear the hand-off state: the dashboard's "solve the captcha" CTA must not
          // outlive the pause it describes — a CTA for a board we already left is the
          // same class of lie as a campaign that reads "live" after it died (#98).
          await sendMsg({ type: "DETECTION_CLEARED" });
          // Hand the walk on the way every other spent platform does. PLATFORM_EXHAUSTED
          // owns the ledger AND the stop: if this was the only platform left (or a
          // single-platform run, where switching boards was never consented to), it
          // stops the campaign itself — so the old behaviour survives exactly where it
          // was the honest one.
          await sendMsg({
            type: "PLATFORM_EXHAUSTED",
            platform: plat,
            reason: "captcha not cleared in 5 min",
          });
          return;
        }
      }
    }

    // Consent wall ("we've updated our Terms", "Accept to continue"). It blocks the page
    // the same way a captcha does, but it is ACCOUNT-wide, not posting-specific: skipping
    // to the next job walks straight into the same modal and burns the queue one approved
    // pick at a time. So pool mode pauses here too — the opposite of the captcha branch
    // above, and deliberately so.
    {
      const gate = detectConsentGate();
      if (gate.gated) {
        const site = window.location.hostname.replace(/^www\./, "");
        log(`⚠️ ${site} wants you to accept its terms — click "${gate.label}" in this window; the campaign resumes on its own.`, "err");
        // The pool window runs unfocused and unwatched, so the in-window line above may
        // never be read. Put the same sentence in the dashboard feed.
        logBackend(`⏸ Paused — ${site} is asking you to accept its terms. Click "${gate.label}" in the campaign window.`, "warn");
        await sendMsg({
          type: "DETECTION_TRIPPED",
          data: {
            signal: `consent:${gate.label}`,
            // `kind` splits the hand-off copy: this is not a "prove you're human" moment,
            // and telling someone to solve a captcha that isn't there sends them hunting.
            kind: "terms",
            action: `Click "${gate.label}" in the campaign window`,
            url: window.location.href,
            phase: detectPhase(),
          },
        });
        // Same clock as the captcha above, same reason: a terms wall is account-wide
        // within THIS board only, so once we have somewhere else to go, holding the
        // whole run for two hours buys nothing.
        const _gateStart = Date.now();
        while (Date.now() - _gateStart < CONSENT_WAIT_MS) {
          await sleep(8000);
          if (!(await isCampaignRunning())) return; // user stopped it themselves
          if (!detectConsentGate().gated) {
            log("Terms accepted — resuming campaign", "ok");
            logBackend("▶ Terms accepted — campaign resumed", "info");
            await sendMsg({ type: "DETECTION_CLEARED" });
            break;
          }
        }
        if (detectConsentGate().gated) {
          const plat = detectPlatform();
          log("Terms not accepted in 5 min — moving on to another platform", "err");
          logBackend(
            `${site} still wants its terms accepted after 5 minutes — moving on to another platform. ` +
            "Accept them any time; we'll come back to this board on the next run.",
            "warn");
          await sendMsg({ type: "DETECTION_CLEARED" });
          await sendMsg({
            type: "PLATFORM_EXHAUSTED",
            platform: plat,
            reason: "terms not accepted in 5 min",
          });
          return;
        }
        // Accepting usually navigates or re-renders the page under us, so the phase we
        // detected before the pause is stale. Fall through — `detectPhase()` below runs
        // after this block — and clear lastPhase so the observer isn't suppressed if the
        // page re-renders into the same phase name. (Same shape as the captcha branch:
        // the tick continues, it doesn't hand back and hope for another one.)
        lastPhase = "";
      }
    }

    // Login wall: auto-apply is impossible if the user isn't logged into the
    // platform. Detect it, report it (so the dashboard flips to "not connected"),
    // and PAUSE — the user logs in in this same window and we resume. Same pattern
    // as the CAPTCHA hand-off: we never fake-submit against a logged-out session.
    {
      const authPlatform = detectPlatform();
      // ATS guest-apply pages (Greenhouse/Lever) NEVER require a login — you apply as a
      // guest. Skip the login-wall check for them: otherwise a stray "Sign in" link on the
      // posting reads as logged_out and parks the campaign in the 2h login-pause loop below
      // (looks like a dead 6-7-min+ hang on a zero-touch GH apply). Mirrors sessionWarmup's
      // greenhouse/lever guard.
      const isAtsGuest = authPlatform === "greenhouse" || authPlatform === "lever" || authPlatform === "ashby";
      if (authPlatform === "indeed") await settleIndeedAuthTransit();
      const authStatus = isAtsGuest ? "connected" : detectPlatformAuth(authPlatform);
      if (authStatus === "logged_out") {
        await reportPlatformAuth();
        const name = platformLabel();
        log(`⚠️ Not signed into ${name}. Log in (or create an account) in this window — the campaign resumes automatically once you're in.`, "err");
        await sendMsg({ type: "PLATFORM_LOGIN_REQUIRED", platform: authPlatform, url: window.location.href, host: window.location.hostname });
        // Third door, same lock as the captcha and the terms gate — and the same answer.
        // Being signed out of Indeed says nothing about Greenhouse (ATS boards apply as
        // a guest anyway), so this is a reason to leave the BOARD, not the run.
        const _loginPauseStart = Date.now();
        while (Date.now() - _loginPauseStart < HUMAN_WALL_WAIT_MS) {
          await sleep(8000);
          if (!(await isCampaignRunning())) return; // user stopped it themselves
          if (detectPlatformAuth(authPlatform) === "connected") {
            log(`Signed into ${name} — resuming campaign`, "ok");
            await reportPlatformAuth();
            break;
          }
        }
        if (detectPlatformAuth(authPlatform) === "logged_out") {
          log(`Still not signed into ${name} after 5 min — moving on to another platform`, "err");
          logBackend(
            `Still signed out of ${name} after 5 minutes — moving on to another platform. ` +
            "Sign in any time; we'll come back to this board on the next run.",
            "warn");
          await sendMsg({
            type: "PLATFORM_EXHAUSTED",
            platform: authPlatform,
            reason: "not signed in after 5 min",
          });
          return;
        }
      }
    }

    const phase = detectPhase();

    // Reaching a known phase means we're on track — clear the ZR recovery counter.
    if (phase !== "unknown") {
      storageSet({ zrRecoveries: 0 }).catch(() => {});
    }

    try {
      switch (phase) {
        case "list": {
          // POOL SWIPE RUN: the walk navigates straight to single-job pages
          // (/viewjob?jk=…). Landing on a SEARCH list here means Indeed redirected a
          // dead/invalid posting to the SERP (live-test 2026-07-27: viewjob →
          // /jobs?q=&l=remote&vjk=…). Running phase1 would walk the search and apply
          // jobs the user never swiped — the exact footgun. Skip the item instead.
          if ((await storageGet("atsPlatform")).atsPlatform === "pool") {
            logBackend("Posting looks closed (Indeed sent us to search) — skipping to your next pick", "warn");
            await sendMsg({ type: "ATS_JOB_DONE" });
            break;
          }
          if (detectPlatform() === "linkedin") { await phase1_linkedinList(); break; }
          await phase1_jobList();
          break;
        }
        case "detail":
          if (detectPlatform() === "linkedin") { await phase2_linkedinDetail(); break; }
          await phase2_jobDetail();
          break;
        case "form": {
          const _p = detectPlatform();
          if (_p === "greenhouse" || _p === "lever" || _p === "ashby") {
            await phase_ats(_p);
            await returnToBoardAfterAts(); // P4: continue the board campaign if we came from one
          } else if (_p === "linkedin") {
            await phase3_linkedinForm();
          } else {
            await phase3_fillForm();
          }
          break;
        }
        default:
          // Unknown page. Pool swipe run: never "recover" into a board SEARCH (that
          // walk applies un-swiped jobs) — skip this queue item and advance the pool.
          if ((await storageGet("atsPlatform")).atsPlatform === "pool") {
            if (await isCampaignRunning()) {
              // EXCEPT the warm-landing homepage: for Indeed/ZR pool jobs background opens
              // the platform HOMEPAGE first (to pass Cloudflare), then sessionWarmup
              // navigates to the picked job. That homepage (pathname "/") is an "unknown"
              // phase — skipping here would burn an approved pick before warmup even runs
              // (live 2026-07-28: "Couldn't open this job page (www.indeed.com)" ate a job).
              // Leave it to warmup; only skip a genuinely broken job page.
              const onHomeRoot = location.pathname === "/" || location.pathname === "";
              if (onHomeRoot) break;
              // A confirmation page is not a broken job page: the submit landed and killed
              // the context that would have recorded it (#217, Amwell 09-21).
              if (await recordWokeOnPostApply()) break;
              // Async ATS forms (Ashby/Greenhouse React) render the fields LATE, especially in
              // a throttled background window — detectPhase sees no field yet and would skip a
              // LIVE job as "couldn't open". POLL for hydration (~20s) before giving up; if the
              // form appears, dispatch straight to phase_ats (2026-08-04: Ashby /application
              // forms were fine but late → every Ashby job wrongly skipped → 0 submits).
              const _h = location.hostname;
              if (/ashbyhq\.com|greenhouse\.io|lever\.co/.test(_h) && /\/application|\/apply/.test(location.pathname)) {
                let _ready = false;
                for (let i = 0; i < 8 && !_ready; i++) {
                  await sleep(2500);
                  if (!(await isCampaignRunning())) break;
                  _ready = detectPhase() === "form";
                }
                if (_ready) {
                  const _pp = detectPlatform();
                  if (_pp === "greenhouse" || _pp === "lever" || _pp === "ashby") {
                    await phase_ats(_pp);
                    await returnToBoardAfterAts();
                    break;
                  }
                }
              }
              logBackend(`Couldn't open this job page (${location.hostname}) — skipping to your next pick`, "warn");
              await sendMsg({ type: "ATS_JOB_DONE" });
            }
            break;
          }
          // Auto-ATS walk (greenhouse/lever, non-pool): an unknown page here is a DEAD/closed
          // posting — e.g. GH redirects an expired job to job-boards.greenhouse.io/<org>?error=
          // true (board root, no form). Without advancing, the walk STALLS on it and re-inits
          // forever → applied=0 (live 2026-07-31 on reddit?error=true). Skip + advance the queue.
          {
            const _atsP = (await storageGet("atsPlatform")).atsPlatform;
            if ((_atsP === "greenhouse" || _atsP === "lever" || _atsP === "ashby") && (await isCampaignRunning())) {
              // A thank-you page is not a closed posting: the submit landed and killed the
              // context that would have recorded it (live 2026-09-27, Tia /confirmation).
              if (await recordWokeOnPostApply()) break;
              logBackend(`Skipping (posting closed/errored on ${location.hostname}) — next job`, "warn");
              await sendMsg({ type: "ATS_JOB_DONE" });
              break;
            }
          }
          // On ZipRecruiter this is usually /jobseeker/home or a session redirect —
          // recover to the search instead of stalling forever.
          if (detectPlatform() === "ziprecruiter" && (await isCampaignRunning())) {
            await recoverZipRecruiterPhase();
          }
          break;
      }
    } catch (err) {
      log(`Error in ${phase} phase: ${err.message}`, "err");
      safeSend({ type: "STEP_FAILED", data: { phase, error: err.message } });
      // Try to recover by skipping to next job
      await sleep(humanDelay(3000, 5000));
      await skipToNextJob();
    }
  }

  // =========================================================================
  // MutationObserver — detect Indeed SPA content changes
  // =========================================================================

  let phaseDebounce = null;
  let lastPhase = "";

  const observer = new MutationObserver(() => {
    clearTimeout(phaseDebounce);
    phaseDebounce = setTimeout(async () => {
      if (!(await isCampaignRunning())) return;

      const phase = detectPhase();
      // Only re-run if phase changed (avoid re-triggering on minor DOM changes)
      if (phase !== lastPhase && phase !== "unknown") {
        lastPhase = phase;
        runPhase();
      }

      // Special case: form appeared while on detail page
      if (phase === "form" && lastPhase !== "form") {
        lastPhase = "form";
        runPhase();
      }
    }, 1000);
  });

  // ---- CAPTURE KIT -----------------------------------------------------------------------
  // LinkedIn capture kit (docs/handoff/linkedin.md). DEV ONLY: answers just on a LinkedIn tab
  // with the `linkedinBeta` flag on, and only when the popup's dev section asks. It turns the
  // page the human is looking at into an HTML file for test fixtures — the repo rule is that
  // fixtures are CAPTURED from real pages, never written from memory (a made-up fixture once
  // passed 12 checks and matched nothing live).
  //
  // What leaves the page, and what does not:
  //   * <script> bodies, hidden <code> JSON blobs (LinkedIn ships its API payloads there, the
  //     member's profile included), typed input values, textarea and contenteditable text;
  //   * emails and phone-like numbers (maskPii), profile slugs (/in/<slug>), member URNs;
  //   * the member's own name/email/phone/address/profile URL from the cached HireDrop
  //     profile, and the name on the nav "Me" photo, wherever they appear as text;
  //   * the "Me" menu / identity card containers and profile photos.
  // Masking is best effort against a DOM we have not seen yet — the file must be READ before
  // it is committed. Nothing is sent anywhere: the popup saves it as a download.

  const CAPTURE_MEMBER_SELECTORS = [
    ".global-nav__me", ".global-nav__me-content", "[data-test-global-nav-me]",
    ".feed-identity-module", ".profile-card", ".artdeco-entity-lockup--member",
  ].join(", ");
  // Input types whose value is a fixed option or a button label, not something typed.
  const CAPTURE_KEEP_VALUE_TYPES = new Set(["radio", "checkbox", "submit", "button", "reset", "image"]);
  // Attributes a human reads — phone-like numbers there are masked like text. Everything else
  // keeps its digits: job ids (data-occludable-job-id, /jobs/view/<id>) ARE the fixture.
  const CAPTURE_TEXT_ATTRS = new Set(["aria-label", "title", "alt", "placeholder", "aria-description", "aria-valuetext", "data-tooltip"]);

  function captureEscapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // Pure: the strings that identify the member, longest first so "Jane Doe" goes before "Jane".
  function captureIdentityTerms(profile, doc) {
    const p = profile || {};
    const out = new Set();
    const add = (v) => {
      const s = String(v || "").replace(/\s+/g, " ").trim();
      if (s.length >= 3) out.add(s);
    };
    add(p.name); add(p.first_name); add(p.last_name);
    add([p.first_name, p.last_name].filter(Boolean).join(" "));
    for (const part of String(p.name || "").split(/\s+/)) add(part);
    add(p.email); add(p.phone); add(p.street_address);
    for (const u of [p.linkedin_url, p.linkedin]) {
      const m = /\/in\/([^/?#\s]+)/i.exec(String(u || ""));
      if (m) add(m[1]);
    }
    if (doc) {
      for (const img of doc.querySelectorAll("img.global-nav__me-photo, .global-nav__me img, [data-test-global-nav-me] img")) {
        const alt = (img.getAttribute("alt") || "").replace(/^photo of\s+/i, "");
        add(alt);
        for (const part of alt.split(/\s+/)) add(part);
      }
    }
    return Array.from(out).sort((a, b) => b.length - a.length);
  }

  function captureMaskTerms(s, terms) {
    let v = s;
    for (const t of terms || []) {
      const edgeA = /^\w/.test(t) ? "\\b" : "";
      const edgeB = /\w$/.test(t) ? "\\b" : "";
      v = v.replace(new RegExp(edgeA + captureEscapeRe(t) + edgeB, "gi"), "<member>");
    }
    return v;
  }

  function captureMaskIds(s) {
    return s
      .replace(/\/in\/[^/?#"'\s<>]+/gi, "/in/redacted")
      .replace(/urn:li:(fsd_profile|fs_miniProfile|fsd_miniProfile|fs_profile|member|person|profile):[A-Za-z0-9_-]+/g, "urn:li:$1:redacted");
  }

  function captureScrubText(s, terms) {
    return captureMaskTerms(captureMaskIds(maskPii(s)), terms);
  }

  function captureScrubAttr(name, value, terms) {
    let v = captureMaskIds(value.replace(/\S+@\S+\.\S+/g, "<email>"));
    if (CAPTURE_TEXT_ATTRS.has(name)) v = maskPii(v);
    return captureMaskTerms(v, terms);
  }

  // Scrub a subtree in place: an Element, a detached clone, or a <template>'s content.
  function captureScrub(root, terms) {
    const els = root.querySelectorAll ? Array.from(root.querySelectorAll("*")) : [];
    if (root.nodeType === 1) els.unshift(root);
    for (const el of els) {
      const tag = el.tagName.toLowerCase();
      if (tag === "template" && el.content) captureScrub(el.content, terms);
      if (tag === "script") el.textContent = "";
      // LinkedIn's server-rendered API payloads live in hidden <code> elements — as JSON
      // text, escaped JSON, or JSON inside an HTML comment (textContent misses that one).
      // A selector fixture never needs a <code> body, so every one is emptied.
      if (tag === "code") el.textContent = "";
      if (tag === "textarea") el.textContent = "";
      if (tag === "input") {
        const type = (el.getAttribute("type") || "text").toLowerCase();
        if (!CAPTURE_KEEP_VALUE_TYPES.has(type)) el.removeAttribute("value");
      }
      if (tag === "option" && el.hasAttribute("value")) el.setAttribute("value", captureScrubText(el.getAttribute("value"), terms));
      const ce = el.getAttribute("contenteditable");
      if (ce !== null && ce !== "false") el.textContent = "";
      if (tag === "meta" && /csrf|token|session|member|user/i.test(`${el.getAttribute("name") || ""} ${el.getAttribute("http-equiv") || ""}`)) {
        el.removeAttribute("content");
      }
      if (tag === "img" && /profile-(display|framed)photo/i.test(`${el.getAttribute("src") || ""} ${el.getAttribute("srcset") || ""}`)) {
        el.removeAttribute("src"); el.removeAttribute("srcset");
      }
      for (const a of Array.from(el.attributes)) {
        // data-csrf, data-token, …: request credentials, never needed by a selector.
        if (/csrf|token|session/i.test(a.name)) { el.removeAttribute(a.name); continue; }
        // Inline photos (style="background-image:url(…)") — the member's face among them.
        if (a.name === "style" && /url\(/i.test(a.value)) {
          el.setAttribute("style", a.value.replace(/url\([^)]*\)/gi, "url(redacted)"));
          continue;
        }
        const nv = captureScrubAttr(a.name, a.value, terms);
        if (nv !== a.value) el.setAttribute(a.name, nv);
      }
    }
    // The member's own identity card / nav "Me" menu: keep the structure, drop the words.
    if (root.querySelectorAll) {
      for (const box of root.querySelectorAll(CAPTURE_MEMBER_SELECTORS)) {
        const tw = (box.ownerDocument || box).createTreeWalker(box, 4 /* SHOW_TEXT */);
        for (let n = tw.nextNode(); n; n = tw.nextNode()) if (n.nodeValue.trim()) n.nodeValue = "<member>";
        for (const img of box.querySelectorAll("img")) {
          img.removeAttribute("src"); img.removeAttribute("srcset"); img.setAttribute("alt", "<member>");
        }
      }
    }
    const owner = root.ownerDocument || root;
    // Comments carry data too (LinkedIn has shipped JSON payloads as <!--{…}-->) and are
    // invisible to the text walk below — drop them all.
    const cw = owner.createTreeWalker(root, 128 /* SHOW_COMMENT */);
    const comments = [];
    for (let n = cw.nextNode(); n; n = cw.nextNode()) comments.push(n);
    for (const c of comments) c.parentNode && c.parentNode.removeChild(c);
    const tw = owner.createTreeWalker(root, 4 /* SHOW_TEXT */);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      const parent = n.parentNode && n.parentNode.nodeName ? n.parentNode.nodeName.toLowerCase() : "";
      if (parent === "style" || parent === "script") continue; // CSS digits are not phones
      const nv = captureScrubText(n.nodeValue, terms);
      if (nv !== n.nodeValue) n.nodeValue = nv;
    }
  }

  // The whole page as a standalone, PII-masked HTML string. Open shadow roots go in as
  // declarative <template shadowrootmode="open"> so the file renders like the page did;
  // closed ones are invisible to us and stay out.
  function captureSanitizedHtml(doc, terms) {
    const root = doc.documentElement;
    const clone = root.cloneNode(true);
    const origAll = root.querySelectorAll("*");
    const cloneAll = clone.querySelectorAll("*");
    for (let i = 0; i < origAll.length && i < cloneAll.length; i++) {
      const sr = origAll[i].shadowRoot;
      if (!sr) continue;
      const holder = doc.createElement("div");
      holder.innerHTML = sr.innerHTML;
      captureScrub(holder, terms);
      const tpl = doc.createElement("template");
      tpl.setAttribute("shadowrootmode", "open");
      tpl.innerHTML = holder.innerHTML;
      cloneAll[i].insertBefore(tpl, cloneAll[i].firstChild);
    }
    captureScrub(clone, terms);
    return clone.outerHTML;
  }

  function captureEasyApplyModal(doc) {
    const strict = doc.querySelector(".jobs-easy-apply-modal, .jobs-easy-apply-content");
    if (strict) return strict.closest("[role='dialog']") || strict;
    for (const d of doc.querySelectorAll("[role='dialog']")) {
      const label = `${d.getAttribute("aria-label") || ""} ${d.getAttribute("aria-labelledby") ? (doc.getElementById(d.getAttribute("aria-labelledby")) || {}).textContent || "" : ""}`;
      if (/easy apply|apply to/i.test(label) || d.querySelector(".jobs-easy-apply-form-section__grouping, [data-easy-apply-next-button]")) return d;
    }
    return null;
  }

  // Pure (given the memo): which page this is, for the file name. Modal steps are numbered
  // by DISTINCT step state seen in this tab, so capturing the same step twice keeps its N.
  function capturePageKind(url, doc, memo) {
    const m = memo || { states: [] };
    const modal = captureEasyApplyModal(doc);
    const text = (modal ? modal.textContent : "") || "";
    if (/\/post-apply\b/i.test(url) || (modal && /(your )?application (was )?sent/i.test(text))) {
      m.states = [];
      return "confirmation";
    }
    if (modal) {
      const heading = ((modal.querySelector("h2, h3") || {}).textContent || "").replace(/\s+/g, " ").trim();
      const bar = modal.querySelector("[role='progressbar']");
      const progress = bar ? bar.getAttribute("aria-valuenow") || "" : "";
      const fields = modal.querySelectorAll("input, select, textarea").length;
      const sig = `${heading}|${progress}|${fields}`;
      let idx = m.states.indexOf(sig);
      if (idx < 0) { m.states.push(sig); idx = m.states.length - 1; }
      return `modal-step-${idx + 1}`;
    }
    m.states = [];
    if (/\/jobs\/(search|collections)\b/i.test(url)) return "search";
    if (/\/jobs\/view\//i.test(url)) return "view";
    return "other";
  }

  function captureTimestamp(d) {
    return d.toISOString().replace(/\.\d+Z$/, "Z").replace(/:/g, "-");
  }

  const _captureMemo = { states: [] };

  async function captureLinkedInPage() {
    if (detectPlatform() !== "linkedin") return { ok: false, error: "not_linkedin" };
    const s = await storageGet(["linkedinBeta", "profile"]);
    if (s.linkedinBeta !== true) return { ok: false, error: "flag_off" };
    const terms = captureIdentityTerms(s.profile || {}, document);
    const kind = capturePageKind(window.location.href, document, _captureMemo);
    let ver = "?"; try { ver = chrome.runtime.getManifest().version; } catch { /* context gone */ }
    // Not maskPii: the job id in /jobs/view/<id>/ reads as a phone number to it.
    const where = captureMaskTerms(captureMaskIds(window.location.origin + window.location.pathname), terms);
    const header =
      `<!-- HireDrop capture kit · ext ${ver} · ${new Date().toISOString()} · ${kind} · ${where}\n` +
      "     PII masked best-effort (emails, phones, input values, scripts, member name/links).\n" +
      "     READ IT before committing as a fixture. -->\n";
    const html = "<!DOCTYPE html>\n" + header + captureSanitizedHtml(document, terms);
    const filename = `linkedin-${kind}-${captureTimestamp(new Date())}.html`;
    return { ok: true, kind, filename, html, bytes: html.length };
  }
  // ---- END CAPTURE KIT -------------------------------------------------------------------

  // =========================================================================
  // Message listener
  // =========================================================================

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    switch (msg.type) {
      case "HD_LINKEDIN_CAPTURE":
        // Async answer: keep the channel open (return true). The kit refuses off LinkedIn and
        // with the flag off, so this is inert on every page the manifest injects into.
        captureLinkedInPage().then(sendResponse, (e) => sendResponse({ ok: false, error: String((e && e.message) || e) }));
        return true;

      case "CAMPAIGN_STARTED":
        log("Campaign started — beginning automation", "ok");
        lastPhase = "";
        _tabVerdict = null; // a (re)start may have moved the campaign tab — ask afresh
        runPhase(); // gated: runPhase → mayAutomateThisTab
        sendResponse({ ok: true });
        break;

      case "CAMPAIGN_STOPPED":
        log("Campaign stopped", "");
        lastPhase = "";
        sendResponse({ ok: true });
        break;

      default:
        sendResponse({ ok: true });
    }
  });

  // =========================================================================
  // Init — start when page loads
  // =========================================================================

  async function init() {
    // OURA-BUG hardening (GLOBAL_PLAN P1 polish c): the walk once froze because the next
    // queue page produced NO log at all — init either hung or died silently. Two rules now:
    // (1) if a campaign is on, BEACON before any await that can hang, so "script alive on
    // this page" always reaches the dashboard; (2) any init crash is reported, not lost.
    try {
      // Report whether the user is logged into this platform (for the dashboard's
      // connection status). Runs FIRST — before the selectors fetch — so a slow or
      // failing backend round-trip can never block login detection. Fire-and-forget.
      reportPlatformAuth();
      // …and keep it fresh while the tab lives (SPA logins, expiring sessions).
      watchPlatformAuth();

      // Submit belt — BEFORE every gate below. A full-page ATS submit (Greenhouse →
      // /confirmation) wakes a fresh script here; if this tab is not the campaign tab, or
      // the run has stopped since the click, nothing after this line would record the
      // application. Records only: no queue advance, no automation.
      await recordPendingSubmitOnConfirmation();

      let campaignOn = await isCampaignRunning();
      // Only the campaign's OWN tab may automate. Chrome restores the previous session's
      // windows on restart, so a stopped run's job tabs come back to life, see
      // campaignRunning=true, and all start walking at once — nine of them raced each
      // other on 08-15, stepping on the live run's navigation. `known:false` (no campaign
      // tab recorded) is not permission either: the human's own ZipRecruiter tab got
      // paginated out from under them that way. Fail OPEN on no answer. The answer is
      // cached and runPhase enforces it for every caller (mayAutomateThisTab).
      if (campaignOn && !(await mayAutomateThisTab())) campaignOn = false;
      if (campaignOn) {
        // Version in the line = proof of WHICH content.js is injected (store vs unpacked,
        // pre/post reload) — the 08-15 double-install cost a whole run to "assumed 1.4.4".
        let ver = "?"; try { ver = chrome.runtime.getManifest().version; } catch { /* context gone */ }
        logBackend(`Content script alive on ${location.hostname}${location.pathname.slice(0, 40)} — resuming (ext ${ver})`, "info");
      }

      // Pull DOM selectors from backend (cached 24h) — Phase 4.1
      await loadSelectors();

      // Start observing DOM changes
      if (document.body) {
        observer.observe(document.body, { childList: true, subtree: true });
      }

      // Check if campaign is already running (e.g., page reload)
      if (campaignOn && (await isCampaignRunning())) {
        const platformName = platformLabel();
        log("Campaign active — resuming on this page", "ok");
        logBackend(`Extension active on ${platformName} — starting automation`, "info");
        await sleep(humanDelay(2000, 3000));
        // Entered through Indeed's sign-in page (platformEntryUrl)? Let the renewal
        // redirect happen before warmup gets a chance to navigate the tab elsewhere.
        if (detectPlatform() === "indeed") await settleIndeedAuthTransit();
        // One-shot warmup before the very first action. No-op if already
        // warmed up this campaign.
        await sessionWarmup();
        lastPhase = detectPhase();
        runPhase();
      }
    } catch (e) {
      try { logBackend(`⚠️ Init failed on ${location.hostname}: ${e.message}`, "error"); } catch (_) {}
    }
  }

  // Wait for page to be ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    setTimeout(init, 1000);
  }

  // Screenshot ping — keeps the service worker alive and triggers a capture
  // every 2.5 s while this page is open. background.js only sends to backend
  // when campaignRunning is true, so this is a no-op outside campaigns.
  //
  // The number USED to be 300, i.e. 3.3 messages per second, on every tab matching the
  // manifest — including the user's own Indeed/LinkedIn browsing tabs, forever, campaign or
  // not. That is the "service worker kept alive on stale code" trap this codebase documents
  // elsewhere, paid for in battery on every open job page. 2500 matches the comment that was
  // always here, and 30s is the MV3 idle limit, so the keep-alive still has 12x the margin
  // it needs.
  const SCREENSHOT_PING_MS = 2500;
  const _screenshotPing = setInterval(() => {
    safeSend({ type: "CAPTURE_SCREENSHOT" });
  }, SCREENSHOT_PING_MS);
  // `pagehide`, not `unload`: some ATS hosts (Greenhouse) block `unload` via
  // Permissions-Policy, which spams a console violation on every job page. pagehide
  // is the modern, un-blocked equivalent and fires on navigation all the same. The
  // interval dies with the page context anyway — this is just tidy cleanup.
  window.addEventListener("pagehide", () => clearInterval(_screenshotPing));
})();
