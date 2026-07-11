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

  const MAX_APPLICATIONS_PER_PLATFORM = 50;

  // =========================================================================
  // Platform detection
  // =========================================================================

  function detectPlatform() {
    const host = window.location.hostname;
    if (host.includes("ziprecruiter.com")) return "ziprecruiter";
    if (host.includes("greenhouse.io")) return "greenhouse";
    if (host.includes("lever.co")) return "lever";
    return "indeed";
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
  // =========================================================================

  function detectPlatformAuth(platform) {
    const p = platform || detectPlatform();
    if (p === "indeed") {
      if (document.querySelector(
        '[data-gnav-element-name="SignIn"], a[href*="secure.indeed.com/auth"], a[href*="/account/login"]'
      )) return "logged_out";
      if (document.querySelector(
        '[data-gnav-element-name="AccountMenu"], [data-gnav-element-name="SignOut"], [data-gnav-element-name="Resume"]'
      )) return "connected";
      return "unknown";
    }
    if (p === "ziprecruiter") {
      // The "Log In" link is server-rendered in the header whenever logged out.
      if (document.querySelector('a[href*="/authn/login"]')) return "logged_out";
      if (document.querySelector('a[href*="/authn/logout"], a[href*="/candidate/"]')) return "connected";
      // No login link + a rendered header ⇒ treat as connected (logged-in nav
      // hides the login link and shows an account menu we can't always name).
      if (document.querySelector('header, [data-testid="header"], nav')) return "connected";
      return "unknown";
    }
    return "unknown";
  }

  // Persist + report the current platform's login state. Runs on every content
  // script load (cheap) so status stays fresh whenever the user visits the site.
  // The header/nav that carries the login signal can render slightly after
  // document_idle, so poll a few times for a DEFINITIVE (non-unknown) answer
  // before giving up — otherwise an early "unknown" would never get corrected.
  async function reportPlatformAuth() {
    const platform = detectPlatform();
    let status = detectPlatformAuth(platform);
    for (let i = 0; i < 8 && status === "unknown"; i++) {
      await sleep(1000);
      status = detectPlatformAuth(platform);
    }
    if (status === "unknown") return status; // still indeterminate — don't store noise
    try {
      const store = await chrome.storage.local.get("platformConnections");
      const conns = store.platformConnections || {};
      conns[platform] = { status, checkedAt: new Date().toISOString() };
      await chrome.storage.local.set({ platformConnections: conns });
      chrome.runtime.sendMessage({ type: "PLATFORM_AUTH", platform, status }).catch(() => {});
    } catch { /* storage/runtime unavailable — ignore */ }
    return status;
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
    chrome.runtime.sendMessage({ type: "LOG", text, cls: cls || "" });
  }

  function logBackend(text, level) {
    chrome.runtime.sendMessage({ type: "LOG_BACKEND", text, level: level || "info" }).catch(() => {});
  }

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
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
    const data = await chrome.storage.local.get("campaignRunning");
    return !!data.campaignRunning;
  }

  // Phase 5.4 — Session warmup. The pattern "open page → instantly start
  // automating clicks" never happens for a real user. They land on the
  // search page, glance over a few cards, scroll, sometimes scroll back,
  // *then* engage. Detectors that trigger on engage-time-from-pageload
  // (anything < 3-5s is suspicious) catch this. We run a one-shot
  // warmup the first time content.js sees a running campaign on this
  // page, then mark it done so reloads/page transitions don't re-warmup.
  async function sessionWarmup() {
    const flag = await chrome.storage.local.get("campaignWarmedUp");
    if (flag.campaignWarmedUp) return;

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
    const elapsed = Date.now() - startedAt;
    await chrome.storage.local.set({ campaignWarmedUp: true });

    // Navigate to the target search URL if we're not already on it.
    const { campaignTargetUrl } = await chrome.storage.local.get("campaignTargetUrl");
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
    const data = await chrome.storage.local.get(["platformCounts", "todayDate"]);
    const today = new Date().toISOString().slice(0, 10);
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
  // or simply did nothing. Returns { verified, signal } for activity log.
  async function waitForSubmissionConfirmation(timeoutMs = 20000) {
    const start = Date.now();
    const startUrl = window.location.href;
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
    ];
    // Specific path segments only — broad single words like "submitted"/"success"
    // can appear in intermediate step URLs and cause a false "verified".
    const POSTAPPLY_URL_HINTS = [
      "/applied", "postapply", "post_apply", "post-apply", "thank-you", "thankyou",
      "/success", "/confirmation", "application-submitted", "applysuccess",
    ];

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
        if (bodyText.includes(phrase)) {
          return { verified: true, signal: `text:${phrase.slice(0, 30)}` };
        }
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

  // Quick-set for long text (cover letters) — no char-by-char
  function quickSet(el, value) {
    if (!el || !value) return false;
    el.focus();
    setNativeValue(el, value);
    el.dispatchEvent(new Event("blur", { bubbles: true }));
    return true;
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
    const labels = document.querySelectorAll("label");
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
      const el = document.querySelector(sel);
      // Scripts are never offsetParent-visible — query separately above
      // doesn't apply here, but a present <script> still matters.
      const isScript = sel.startsWith("script[");
      if (el && (isScript || el.offsetParent !== null)) {
        return { detected: true, signal: `dom:${sel.slice(0, 40)}` };
      }
    }
    for (const pat of det.scriptSrcPatterns || []) {
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

  async function loadSelectors() {
    const platform = detectPlatform();
    const cacheKey = `selectors_${platform}`;
    try {
      const cached = await chrome.storage.local.get([cacheKey, `${cacheKey}_at`]);
      const fresh = cached[`${cacheKey}_at`] && Date.now() - cached[`${cacheKey}_at`] < 24 * 3600 * 1000;
      if (fresh && cached[cacheKey]) {
        SELECTORS = cached[cacheKey];
        return;
      }
      const resp = await sendMsg({ type: "GET_SELECTORS", platform });
      if (resp?.selectors) {
        SELECTORS = resp.selectors;
        await chrome.storage.local.set({
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
    const titleEl =
      card.querySelector(".jobTitle a") ||
      card.querySelector("h2.jobTitle a") ||
      card.querySelector("h2 a") ||
      card.querySelector("a[data-jk]");
    const companyEl =
      card.querySelector('[data-testid="company-name"]') ||
      card.querySelector(".companyName") ||
      card.querySelector('[class*="company"]');

    const title = titleEl?.textContent?.trim() || "";
    const company = companyEl?.textContent?.trim() || "";
    const href = titleEl?.getAttribute("href") || "";
    const url = href.startsWith("http") ? href : "https://www.indeed.com" + href;
    let jk = card.getAttribute("data-jk") || titleEl?.getAttribute("data-jk") || "";
    if (!jk && href) {
      const m = href.match(/[?&]jk=([a-z0-9]+)/i);
      if (m) jk = m[1];
    }

    return { title, company, url, jk, clickEl: titleEl };
  }

  async function phase1_jobList() {
    const platform = detectPlatform();
    if (platform === "ziprecruiter") return await phase1_ziprecruiter();
    return await phase1_indeed();
  }

  async function phase1_indeed() {
    if (!(await isCampaignRunning())) return;

    // Guard: if Indeed redirected us to a generic q= page (e.g. from an expired
    // viewjob that auto-redirects), mark the job that caused the redirect as
    // processed and skip to the next pending job. Using skipToNextJob() (not
    // goBackToJobList) preserves the remaining jobs from the current page scan.
    const urlQ = new URL(window.location.href).searchParams.get("q") || "";
    const filtersData = await chrome.storage.local.get(["campaignFilters", "pendingJobs", "currentJobIndex"]);
    const kw = filtersData.campaignFilters?.keywords || [];
    if (kw.length && !urlQ.trim()) {
      log("Redirected to empty search — marking failed job and skipping...", "");
      const jobs = filtersData.pendingJobs || [];
      const idx = filtersData.currentJobIndex || 0;
      const failedJob = jobs[idx];
      if (failedJob?.jk) {
        const seen = await chrome.storage.local.get("processedJobKeys");
        const keys = seen.processedJobKeys || [];
        if (!keys.includes(failedJob.jk)) {
          await chrome.storage.local.set({ processedJobKeys: [...keys, failedJob.jk].slice(-500) });
        }
      }
      await skipToNextJob();
      return;
    }

    const count = await getPlatformCount("indeed");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`Indeed daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}). Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
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
    const easyApplyCards = [];
    const alreadyApplied = await getAppliedUrls();
    const seenKeys = await chrome.storage.local.get("processedJobKeys");
    const processedKeys = new Set(seenKeys.processedJobKeys || []);

    for (const card of cards) {
      if (!isEasilyApplyCard(card)) continue;
      const info = extractCardInfo(card);
      if (!info.title || !info.clickEl) continue;
      if (alreadyApplied.has(info.url)) continue;
      if (info.jk && processedKeys.has(info.jk)) continue;
      easyApplyCards.push(info);
    }

    if (!easyApplyCards.length) {
      log("No new Easy Apply jobs found. Checking next page...", "");
      logBackend("No Easy Apply jobs on this page — going to next", "info");
      await goToNextPage();
      return;
    }

    log(`Found ${easyApplyCards.length} Easy Apply jobs`, "ok");
    logBackend(`Found ${easyApplyCards.length} Easy Apply jobs on page`, "ok");

    // Save pending jobs
    await chrome.storage.local.set({
      pendingJobs: easyApplyCards.map((j) => ({
        title: j.title,
        company: j.company,
        url: j.url,
        jk: j.jk,
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
    const viewjobUrl = firstJob.jk
      ? `https://www.indeed.com/viewjob?jk=${firstJob.jk}`
      : firstJob.url;
    window.location.href = viewjobUrl;
  }

  async function getAppliedUrls() {
    const data = await chrome.storage.local.get("appliedUrls");
    return new Set(data.appliedUrls || []);
  }

  async function addAppliedUrl(url) {
    const data = await chrome.storage.local.get("appliedUrls");
    const urls = data.appliedUrls || [];
    urls.push(url);
    // Keep last 500
    if (urls.length > 500) urls.splice(0, urls.length - 500);
    await chrome.storage.local.set({ appliedUrls: urls });
  }

  // A URL-independent dedup key. Board job URLs carry volatile params (ZR's lk=,
  // tracking) so the same posting can present different URLs across runs. Keying by
  // title|company catches the same job regardless — the robust cross-session guard.
  function jobDedupKey(title, company) {
    return `${(title || "").toLowerCase().replace(/\s+/g, " ").trim()}|${(company || "").toLowerCase().replace(/\s+/g, " ").trim()}`;
  }

  async function getAppliedJobKeys() {
    const data = await chrome.storage.local.get("appliedJobKeys");
    return new Set(data.appliedJobKeys || []);
  }

  async function addAppliedJobKey(title, company) {
    const key = jobDedupKey(title, company);
    if (!key || key === "|") return;
    const data = await chrome.storage.local.get("appliedJobKeys");
    const keys = data.appliedJobKeys || [];
    if (!keys.includes(key)) keys.push(key);
    if (keys.length > 1000) keys.splice(0, keys.length - 1000);
    await chrome.storage.local.set({ appliedJobKeys: keys });
  }

  // Increment the local application count from the CONTENT SCRIPT (not the service
  // worker). MV3 service workers can run stale code after an extension reload, which
  // left the count at 0 despite real submissions. content.js reloads reliably on
  // navigation, so counting here makes the daily cap + count robust regardless of SW
  // state. background's APPLICATION_SAVED no longer increments (backend save only).
  async function recordLocalApplication(platform) {
    const s = await chrome.storage.local.get(["todayCount", "platformCounts", "todayDate"]);
    const today = new Date().toISOString().slice(0, 10);
    const totalCount = (s.todayDate === today ? (s.todayCount || 0) : 0) + 1;
    const platformCounts = s.todayDate === today ? (s.platformCounts || {}) : {};
    platformCounts[platform] = (platformCounts[platform] || 0) + 1;
    await chrome.storage.local.set({ todayCount: totalCount, platformCounts, todayDate: today });
    return platformCounts[platform];
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

  async function phase2_jobDetail() {
    const platform = detectPlatform();
    if (platform === "ziprecruiter") return await phase2_ziprecruiter();
    return await phase2_indeed();
  }

  async function phase2_indeed() {
    if (!(await isCampaignRunning())) return;

    const count = await getPlatformCount("indeed");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`Indeed daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}). Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }

    log("On job detail page — extracting info...", "");
    await sleep(humanDelay(1500, 2500));

    // Extract job info
    const titleEl =
      document.querySelector("h1.jobsearch-JobInfoHeader-title") ||
      document.querySelector('[data-testid="jobsearch-JobInfoHeader-title"]') ||
      document.querySelector("h2.jobTitle") ||
      document.querySelector("h1");
    const companyEl =
      document.querySelector('[data-testid="inlineHeader-companyName"]') ||
      document.querySelector('[data-testid="company-name"]') ||
      document.querySelector(".jobsearch-InlineCompanyRating-companyHeader") ||
      document.querySelector(".companyName");
    const descEl =
      document.querySelector("#jobDescriptionText") ||
      document.querySelector('[class*="jobDescriptionText"]') ||
      document.querySelector(".jobsearch-JobComponent-description");

    const jobTitle = titleEl?.textContent?.trim() || "";
    const jobCompany = companyEl?.textContent?.trim() || "";
    const jobDesc = descEl?.textContent?.trim().slice(0, 1000) || "";
    const jobUrl = window.location.href;

    if (!jobTitle) {
      log("Could not find job title — skipping", "err");
      await skipToNextJob();
      return;
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
      const seen = await chrome.storage.local.get("processedJobKeys");
      const keys = seen.processedJobKeys || [];
      if (keys.includes(dedupeKey)) {
        log(`${jobTitle} — already processed, skipping`, "");
        logBackend(`Skipping duplicate: ${jobTitle}`, "info");
        await skipToNextJob();
        return;
      }
      await chrome.storage.local.set({ processedJobKeys: [...keys, dedupeKey].slice(-500) });
    }

    log(`Job: ${jobTitle} @ ${jobCompany}`, "");

    // Keyword relevance check — skip jobs whose title shares NO words with any
    // campaign keyword. Loosened from strict AND-matching (every word of a phrase
    // had to appear) to OR-matching (at least one keyword word in the title):
    // strict matching skipped clearly-relevant roles, e.g. "Senior Marketing
    // Manager" failed both "healthcare marketing" and "social media manager"
    // because no single phrase matched in full. Still blocks fully off-target
    // titles (e.g. "Provider Relations Specialist") from wasting cover-letter calls.
    {
      const kwData = await chrome.storage.local.get("campaignFilters");
      const kwList = (kwData.campaignFilters?.keywords || []).filter(Boolean);
      if (kwList.length > 0) {
        const titleWords = new Set(jobTitle.toLowerCase().split(/\W+/).filter(w => w.length > 2));
        const keywordWords = new Set(
          kwList.flatMap(phrase => phrase.toLowerCase().split(/\W+/).filter(w => w.length > 2))
        );
        const relevant = [...keywordWords].some(w => titleWords.has(w));
        if (!relevant) {
          log(`${jobTitle} — title doesn't match keywords, skipping`, "");
          logBackend(`Skip (title mismatch): ${jobTitle} @ ${jobCompany}`, "info");
          await skipToNextJob();
          return;
        }
      }
    }

    // Fit Engine M1 — decide whether to apply at ALL before spending a cover
    // letter + application on a wrong-fit job. Runs after the cheap keyword filter
    // and before the expensive steps. Skips roles the resume clearly can't support
    // (too senior, missing hard requirements) with an honest, logged reason. Fails
    // OPEN: a judge error/timeout returns decision "apply" so it never blocks the
    // campaign. Also improves throughput — no more grinding long forms on bad fits.
    {
      const fit = await sendMsg({
        type: "ASSESS_FIT",
        data: { job_title: jobTitle, company: jobCompany, description: jobDesc },
      });
      if (fit && fit.decision === "skip") {
        const why = (fit.reason || "not a strong fit").slice(0, 160);
        log(`Skipping ${jobTitle} — ${why}`, "");
        logBackend(`⏭️ Skipped (fit ${fit.fit_score ?? "?"}): ${jobTitle} @ ${jobCompany} — ${why}`, "info");
        await skipToNextJob();
        return;
      }
      if (fit && fit.judged) {
        logBackend(`✓ Good fit (${fit.fit_score}): ${jobTitle} @ ${jobCompany}`, "info");
      }
    }

    // Save current job context
    await chrome.storage.local.set({
      currentJobInfo: { title: jobTitle, company: jobCompany, description: jobDesc, url: jobUrl },
    });

    // Generate cover letter
    let coverLetter = "";
    log("Generating cover letter...", "");
    try {
      const clRes = await Promise.race([
        sendMsg({
          type: "GENERATE_COVER_LETTER",
          data: { job_title: jobTitle, company: jobCompany, description: jobDesc },
        }),
        sleep(15000).then(() => ({ error: "timeout" })),
      ]);
      if (clRes && clRes.letter) {
        coverLetter = clRes.letter;
        log("Cover letter generated", "ok");
      } else {
        log("Cover letter generation failed — will use template", "");
      }
    } catch (e) {
      log("Cover letter error: " + e.message, "err");
    }

    await chrome.storage.local.set({ generatedCoverLetter: coverLetter });

    // Find and click the Apply button — poll up to 8 s for async panel load
    await sleep(humanDelay(1000, 2000));

    const applyBtn = await waitForApplyButton(8000);
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
      log(`ZipRecruiter daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}). Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }

    log("Scanning ZipRecruiter for Quick Apply jobs...", "");
    logBackend("Scanning ZipRecruiter job list…", "info");
    await sleep(humanDelay(2000, 3000));

    const alreadyApplied = await getAppliedUrls();
    const seenKeys = await chrome.storage.local.get("processedJobKeys");
    const processedKeys = new Set(seenKeys.processedJobKeys || []);

    // Confirmed real selector: .job_result_two_pane_v2 wraps each job card
    const wrappers = Array.from(document.querySelectorAll(".job_result_two_pane_v2"));
    if (!wrappers.length) {
      log("No job cards found on ZipRecruiter — going to next page", "");
      logBackend("No ZR cards found on this page", "warn");
      await goBackToJobList();
      return;
    }

    // Build base search URL (without lk=) for constructing per-job URLs
    const baseUrl = new URL(window.location.href);
    baseUrl.searchParams.delete("lk");
    const baseSearch = baseUrl.toString();

    const quickApplyJobs = [];
    for (const wrapper of wrappers) {
      // Quick Apply badge: <p class="text-brand ..."> inside <div class="...bg-badge-brand...">
      const badge = wrapper.querySelector("div[class*='bg-badge-brand'] p, .text-brand");
      if (!badge || !/quick\s*apply/i.test(badge.textContent || "")) continue;

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

      quickApplyJobs.push({ title, company, url: jobUrl, jk: uuid });
    }

    if (!quickApplyJobs.length) {
      log("No new Quick Apply jobs found — checking next page...", "");
      logBackend("No Quick Apply jobs on this ZipRecruiter page", "info");
      await goBackToJobList();
      return;
    }

    log(`Found ${quickApplyJobs.length} Quick Apply jobs`, "ok");
    logBackend(`Found ${quickApplyJobs.length} Quick Apply jobs on ZipRecruiter`, "ok");

    await chrome.storage.local.set({
      pendingJobs: quickApplyJobs,
      currentJobIndex: 0,
    });

    const first = quickApplyJobs[0];
    log(`Opening: ${first.title} @ ${first.company}`, "");
    logBackend(`Opening ZipRecruiter job: ${first.title} @ ${first.company}`, "info");
    await sleep(humanDelay(2000, 4000));
    window.location.href = first.url;
  }

  async function phase2_ziprecruiter() {
    if (!(await isCampaignRunning())) return;

    const count = await getPlatformCount("ziprecruiter");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`ZipRecruiter daily limit reached. Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
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
    const companyEl = panel?.querySelector('a[href*="/co/"]');
    const descEl = document.querySelector('[data-testid="job-details-scroll-container"]');

    const jobTitle = titleEl?.textContent?.trim() || "";
    const jobCompany = companyEl?.textContent?.trim() || "";
    const jobDesc = descEl?.textContent?.trim().slice(0, 1000) || "";
    const jobUrl = window.location.href;

    if (!jobTitle) {
      log("Could not find job title on ZipRecruiter — skipping", "err");
      await skipToNextJob();
      return;
    }

    // Dedup — session (processedJobKeys) AND cross-session (appliedUrls). Without
    // the appliedUrls check a job applied in a PREVIOUS campaign got re-applied on
    // the next run — a real duplicate to the employer (seen live: Sushi House twice).
    const dedupeKey = jobUrl.split("?")[0];
    {
      const appliedSet = await getAppliedUrls();
      const appliedJobs = await getAppliedJobKeys();
      if (appliedSet.has(dedupeKey) || appliedSet.has(jobUrl) || appliedJobs.has(jobDedupKey(jobTitle, jobCompany))) {
        log(`${jobTitle} — already applied in a previous run, skipping`, "");
        logBackend(`Skip (already applied): ${jobTitle} @ ${jobCompany}`, "info");
        await skipToNextJob();
        return;
      }
      const seen = await chrome.storage.local.get("processedJobKeys");
      const keys = seen.processedJobKeys || [];
      if (keys.includes(dedupeKey)) {
        log(`${jobTitle} — already processed, skipping`, "");
        await skipToNextJob();
        return;
      }
      await chrome.storage.local.set({ processedJobKeys: [...keys, dedupeKey].slice(-500) });
    }

    log(`Job: ${jobTitle} @ ${jobCompany}`, "");

    // Keyword relevance check
    {
      const kwData = await chrome.storage.local.get("campaignFilters");
      const kwList = (kwData.campaignFilters?.keywords || []).filter(Boolean);
      if (kwList.length > 0) {
        const titleWords = new Set(jobTitle.toLowerCase().split(/\W+/).filter((w) => w.length > 2));
        const keywordWords = new Set(
          kwList.flatMap((p) => p.toLowerCase().split(/\W+/).filter((w) => w.length > 2))
        );
        if (![...keywordWords].some((w) => titleWords.has(w))) {
          log(`${jobTitle} — title doesn't match keywords, skipping`, "");
          logBackend(`Skip (title mismatch): ${jobTitle} @ ${jobCompany}`, "info");
          await skipToNextJob();
          return;
        }
      }
    }

    // Fit Engine M1
    {
      const fit = await sendMsg({
        type: "ASSESS_FIT",
        data: { job_title: jobTitle, company: jobCompany, description: jobDesc },
      });
      if (fit && fit.decision === "skip") {
        const why = (fit.reason || "not a strong fit").slice(0, 160);
        log(`Skipping ${jobTitle} — ${why}`, "");
        logBackend(`⏭️ Skipped (fit ${fit.fit_score ?? "?"}): ${jobTitle} @ ${jobCompany} — ${why}`, "info");
        await skipToNextJob();
        return;
      }
      if (fit && fit.judged) {
        logBackend(`✓ Good fit (${fit.fit_score}): ${jobTitle} @ ${jobCompany}`, "info");
      }
    }

    await chrome.storage.local.set({
      currentJobInfo: { title: jobTitle, company: jobCompany, description: jobDesc, url: jobUrl },
    });

    // Generate cover letter
    let coverLetter = "";
    log("Generating cover letter...", "");
    try {
      const clRes = await Promise.race([
        sendMsg({
          type: "GENERATE_COVER_LETTER",
          data: { job_title: jobTitle, company: jobCompany, description: jobDesc },
        }),
        sleep(15000).then(() => ({ error: "timeout" })),
      ]);
      if (clRes && clRes.letter) {
        coverLetter = clRes.letter;
        log("Cover letter generated", "ok");
      } else {
        log("Cover letter generation failed — will use template", "");
      }
    } catch (e) {
      log("Cover letter error: " + e.message, "err");
    }
    await chrome.storage.local.set({ generatedCoverLetter: coverLetter });

    // Find Quick Apply button in the right panel
    await sleep(humanDelay(800, 1500));
    const applyBtn = await waitForZipRecruiterApplyButton(8000);
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
      log(`${jobTitle} — no Quick Apply button found, skipping`, "");
      logBackend(`Skip (no Quick Apply button): ${jobTitle} @ ${jobCompany}`, "info");
      await skipToNextJob();
      return;
    }

    log("Clicking Quick Apply...", "");
    logBackend(`Clicking Quick Apply: ${jobTitle} @ ${jobCompany}`, "info");
    await humanClick(applyBtn);

    // Wait for the apply modal or form to appear
    const formReady = await waitForZipRecruiterForm(15000);
    if (!(await isCampaignRunning())) return;
    if (!formReady) {
      log(`${jobTitle} — no Quick Apply form appeared (external ATS), skipping`, "");
      logBackend(`Skip (no ZR form after 15s): ${jobTitle} @ ${jobCompany}`, "info");
      await skipToNextJob();
      return;
    }

    // Drive phase3 directly — same reason as Indeed: phase2 is on stack,
    // MutationObserver's runPhase() is suppressed by _runPhaseActive guard.
    lastPhase = "form";
    await phase3_fillForm();
  }

  function findZipRecruiterApplyButton() {
    // Confirmed real selector: button[aria-label="Quick Apply"] inside [data-testid="right-pane"]
    const panel = document.querySelector('[data-testid="right-pane"]');
    if (panel) {
      const btn = panel.querySelector('button[aria-label="Quick Apply"]');
      if (btn && btn.offsetParent !== null) return btn;
    }
    // Fallback: any visible "Quick Apply" button anywhere on the page
    for (const el of document.querySelectorAll('button')) {
      if ((el.getAttribute("aria-label") || "").trim() === "Quick Apply" && el.offsetParent !== null) return el;
      if ((el.textContent || "").trim() === "Quick Apply" && el.offsetParent !== null) return el;
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
    if (d.querySelector('input[type="text"], input[type="tel"], input[type="email"], textarea, select, input[type="file"], input[name]')) {
      return true;
    }
    for (const b of d.querySelectorAll("button")) {
      if (b.offsetParent === null) continue;
      const t = ((b.textContent || "") + " " + (b.getAttribute("aria-label") || "")).toLowerCase();
      if (/\b(submit|apply|continue|next|send application)\b/.test(t)) return true;
    }
    return false;
  }

  function findZipRecruiterApplyForm() {
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
      // A dialog with only a Close button = external-apply job, no form to fill.
      const anyDialog = Array.from(document.querySelectorAll('[role="dialog"]')).some((d) => d.offsetParent);
      const onlyClose = anyDialog && !findZipRecruiterApplyForm();
      if (onlyClose && Date.now() - start > 2500) return false; // external — skip fast
      // Redirected away to external ATS
      if (!window.location.hostname.includes("ziprecruiter.com")) return false;
      await sleep(500);
    }
    return false;
  }

  // =========================================================================
  // PHASE 3 — Application Form (multi-step)
  // =========================================================================

  const LABEL_FALLBACKS = {
    firstName: "first name",
    lastName: "last name",
    email: "email",
    phone: "phone",
    coverLetter: "cover letter",
  };

  function findFieldBySelectorsOrLabel(fieldName) {
    // Try direct selectors first (loaded from backend, falls back to constants)
    const fields = SELECTORS.fields || FALLBACK_SELECTORS.fields;
    const selectors = fields[fieldName] || [];
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }
    // Try label-based fallback
    const labelText = LABEL_FALLBACKS[fieldName];
    if (labelText) {
      const el = findByLabel(labelText);
      if (el) return el;
    }
    return null;
  }

  // Fill unanswered radio-button screener questions.
  // Returns count of groups filled. Picks "Yes" for Yes/No groups (covers the
  // most common positive-eligibility questions); picks the first option otherwise.
  async function fillRadioQuestions() {
    const radios = document.querySelectorAll('input[type="radio"]');
    const seen = new Set();
    let filled = 0;

    for (const r of radios) {
      if (seen.has(r.name)) continue;
      seen.add(r.name);

      const group = Array.from(document.querySelectorAll(`input[name="${r.name}"]`));
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

      // Visa / sponsorship questions: pick the option that does NOT require
      // sponsorship. Default picking the first option chose "Yes, I require
      // sponsorship" — a harmful default that also contradicts "I'm authorized to
      // work". (If a user genuinely needs sponsorship they can edit before submit.)
      if (!target && /(sponsor|visa|work permit|require .* immigration)/i.test(groupLabel)) {
        target = (labels.find((l) =>
          /\b(no|not|do not|don'?t)\b/i.test(l.lbl) && /(sponsor|require|need|visa)/i.test(l.lbl)) || {}).el;
        if (!target) target = (labels.find((l) => /^no\b/i.test(l.lbl)) || {}).el;
      }

      // Work-authorization / eligibility ("authorized to work", "right to work",
      // "18 or older", background check, "legally permitted") → affirmative.
      if (!target && /(authoriz|eligible|legally (permitted|authorized|able)|right to work|18 (years|or older)|over 18|able to (work|perform)|consent|agree|background check)/i.test(groupLabel)) {
        target = (labels.find((l) => /^yes\b/i.test(l.lbl)) || {}).el;
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
    const boxes = Array.from(document.querySelectorAll('input[type="checkbox"]'))
      .filter((c) => c.offsetParent && !c.checked);
    let filled = 0;
    for (const c of boxes) {
      const label = getFieldLabel(c) ||
        (c.closest("label, [class*='question' i], fieldset")?.textContent || "");
      const required = c.required || c.getAttribute("aria-required") === "true" ||
        c.getAttribute("aria-invalid") === "true";
      const isAffirmation = /certif|attest|agree|acknowledge|consent|i have read|i understand|\bterms\b|authoriz|confirm/i.test(label);
      // Never tick a NEGATIVE statement or an opt-in ("I do NOT consent…",
      // "I disagree…", "unsubscribe", "opt out") even if required/affirmation-worded.
      if (/\b(not|don'?t|do not|disagree|decline|unsubscribe|opt.?out|refuse)\b/i.test(label)) continue;
      if (!required && !isAffirmation) continue;
      const labelEl = c.id ? document.querySelector(`label[for="${CSS.escape(c.id)}"]`) : null;
      await humanClick(labelEl || c);
      filled++;
      await sleep(humanDelay(200, 500));
    }
    return filled;
  }

  // Fill required text/textarea screener fields that are empty.
  // Employer-defined screener questions can be any type — comments, name, date.
  // We infer the right value from the label text.
  async function fillTextQuestions() {
    const storageData = await chrome.storage.local.get(["profile", "currentJobInfo"]);
    const profile = storageData.profile || {};
    const jobInfo = storageData.currentJobInfo || {};
    const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD

    const inputs = Array.from(document.querySelectorAll('input[type="text"], input[type="number"], textarea'))
      .filter(el => {
        if (!el.offsetParent || el.value.trim()) return false;
        return el.required || el.getAttribute("aria-required") === "true" ||
          el.getAttribute("aria-invalid") === "true" || el.classList.contains("required");
      });

    let filled = 0;
    for (const el of inputs) {
      const rawLabel = getFieldLabel(el);
      const label = rawLabel.toLowerCase();
      const isTextarea = el.tagName === "TEXTAREA";

      let value;
      // Only the applicant's OWN name — not "reference name", "company name",
      // "supervisor/manager/contact name" (those must go to the AI branch).
      if (/\b(first|last|full|your|legal|preferred)\s+name\b|^name$/i.test(label) &&
          !/(reference|company|employer|supervisor|manager|contact|emergency|previous|prior)/i.test(label)) {
        value = `${profile.name || "Applicant"} ${profile.last_name || ""}`.trim();
      } else if (label.includes("date")) {
        value = today;
      } else if (label.includes("email")) {
        value = profile.email || "";
      } else if (label.includes("phone")) {
        value = profile.phone || "";
      } else if (label.includes("salary") || label.includes("compensation") || label.includes("pay") || label.includes("wage")) {
        value = profile.desired_salary || "65000";
      } else if (label.includes("year") || label.includes("experience") || label.includes("how many") || label.includes("how long")) {
        // Only treat as a numeric "years" field for short inputs — an open textarea
        // asking about experience wants prose, which the AI branch handles below.
        if (!isTextarea) value = "2";
      } else if (label.includes("linkedin") || label.includes("portfolio") || label.includes("website") || label.includes("url")) {
        value = profile.linkedin || profile.portfolio || "";
        if (!value) continue;
      } else if (label.includes("city") || label.includes("location")) {
        value = profile.location || "Remote";
      }

      // Open-ended screener question the keyword rules can't map → ask the AI.
      // Gate to real questions (a textarea, or a label that reads like a question)
      // so we don't burn API calls on stray short inputs.
      if (value === undefined) {
        const looksLikeQuestion = isTextarea || rawLabel.includes("?") || rawLabel.length > 20;
        if (looksLikeQuestion && rawLabel) {
          log(`AI answering screener: "${rawLabel.slice(0, 60)}"`, "");
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
      if (isTextarea) quickSet(el, value);
      else setNativeValue(el, value);
      await sleep(humanDelay(150, 400));
      filled++;
    }
    return filled;
  }

  // Demographic / EEO self-identification — we auto-decline (most privacy-preserving,
  // and these are legally voluntary). Matches the question label OR the option set.
  function isDemographicQuestion(label, optionTexts) {
    const demo = /(gender|sex\b|race|ethnic|hispanic|latino|veteran|disab|sexual orientation|transgender|pronoun|national origin|self.?identif)/i;
    const hasDecline = optionTexts.some(t => /(decline|prefer not|don'?t wish|do not wish|not to (answer|say|disclose|identify)|rather not)/i.test(t));
    return demo.test(label) || (hasDecline && demo.test(optionTexts.join(" ")));
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
    // Yes/No eligibility (authorized to work, 18+, background check) → Yes.
    const yes = options.find(o => /^yes$/i.test(o.text));
    if (yes && /(authoriz|eligible|18|over 18|legally|background|consent|agree|able to)/i.test(label)) return yes;
    // Salary range → option closest to desired salary, else the first real option.
    if (/(salary|compensation|pay range|wage|target)/i.test(label)) {
      const want = parseInt(String(profile.desired_salary || "").replace(/\D/g, ""), 10);
      if (want) {
        const withNums = options
          .map(o => ({ o, n: parseInt(o.text.replace(/[^\d]/g, ""), 10) }))
          .filter(x => x.n);
        if (withNums.length) {
          withNums.sort((a, b) => Math.abs(a.n - want) - Math.abs(b.n - want));
          return withNums[0].o;
        }
      }
    }
    return null;
  }

  // Fill required/empty <select> dropdowns. Deterministic for demographic, Yes/No,
  // and salary; AI for ambiguous; first real option as a last resort so a required
  // dropdown can never stall the whole application.
  async function fillSelectQuestions() {
    const storageData = await chrome.storage.local.get(["profile", "currentJobInfo"]);
    const profile = storageData.profile || {};
    const jobInfo = storageData.currentJobInfo || {};

    const selects = Array.from(document.querySelectorAll("select")).filter(s => {
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
    let chosen = pickOptionDeterministic(label, options, profile);
    if (!chosen) {
      log(`AI picking dropdown: "${label.slice(0, 50)}"`, "");
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
    // 2) eligibility / yes-no phrasing → affirmative, never a stray first option.
    if (/(authoriz|eligible|legally|right to work|able to|18 (years|or older)|over 18|consent|agree|background)/i.test(label)) {
      const yes = options.find(o => /^yes\b/i.test(o.text));
      if (yes) return yes;
    }
    // 3) a demographic-looking option set with no neutral → leave unfilled rather
    //    than fabricate an identity value.
    if (/(male|female|non.?binary|hispanic|latino|black|white|asian|veteran|disab)/i.test(options.map(o => o.text).join(" "))) {
      return null;
    }
    // 4) genuinely benign dropdown → first real option.
    return options[0];
  }

  // Fill custom (non-native) dropdowns — Indeed renders demographic/screener
  // dropdowns as DIVs with role="combobox", not <select>. We click to open, read
  // the role="option" list (often portaled), pick, and click. This is what was
  // stalling the demographic page ("Choose an option to continue").
  async function fillComboboxes() {
    const storageData = await chrome.storage.local.get(["profile", "currentJobInfo"]);
    const profile = storageData.profile || {};
    const jobInfo = storageData.currentJobInfo || {};

    const isUnfilled = (c) => {
      if (!c.offsetParent || c.dataset.hdSkip) return false;
      const txt = (c.textContent || "").trim();
      return /^(select|choose|please|--)?\s*(an?\s+)?option?$|^\s*$|select an option|please select|^select$|^choose$/i.test(txt);
    };

    let filled = 0;
    // Re-query each pass instead of iterating a captured snapshot: a React re-render
    // after filling one combobox can detach the others, so cached nodes would no-op.
    // data-hd-skip marks un-openable ones so we don't loop on them forever.
    for (let pass = 0; pass < 14; pass++) {
      const combo = Array.from(
        document.querySelectorAll('[role="combobox"], button[aria-haspopup="listbox"]')
      ).find(isUnfilled);
      if (!combo) break;

      const label = getComboLabel(combo);
      await humanClick(combo);
      await sleep(humanDelay(400, 800));

      // Read options from THIS combobox's OWN menu — a global [role=option] query
      // could grab a different question's still-open menu and apply its answer here.
      const menu = findComboMenu(combo);
      const optEls = menu
        ? Array.from(menu.querySelectorAll('[role="option"], li')).filter(o => o.offsetParent !== null)
        : [];
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

      await humanClick(chosen.el);
      filled++;
      await sleep(humanDelay(400, 800));
      // If it didn't register as filled (still a placeholder), mark skip to avoid a loop.
      if (isUnfilled(combo)) combo.dataset.hdSkip = "1";
    }
    return filled;
  }

  // Find the listbox menu that belongs to a specific combobox (portaled or inline).
  function findComboMenu(combo) {
    const id = combo.getAttribute("aria-controls") || combo.getAttribute("aria-owns");
    if (id) {
      const el = document.getElementById(id);
      if (el && el.offsetParent !== null) return el;
    }
    // Fallback: the most-recently-opened visible listbox.
    const lbs = Array.from(document.querySelectorAll('[role="listbox"]')).filter(l => l.offsetParent !== null);
    return lbs.length ? lbs[lbs.length - 1] : null;
  }

  // Label for a custom combobox: text of the enclosing question block minus the
  // combobox's own placeholder text.
  function getComboLabel(combo) {
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

  function findFormButton() {
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
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }

    // Text-based fallback
    const buttons = document.querySelectorAll("button");
    for (const btn of buttons) {
      const text = btn.textContent?.trim().toLowerCase() || "";
      if (
        (text === "continue" ||
          text === "next" ||
          text.includes("submit application") ||
          text.includes("submit your application") ||
          text.includes("review your application") ||
          text.includes("apply") ||
          text === "review") &&
        btn.offsetParent !== null
      ) {
        return btn;
      }
    }
    return null;
  }

  function isSubmitStep() {
    // Check buttons for submit-intent text
    const buttons = document.querySelectorAll("button");
    for (const btn of buttons) {
      if (btn.offsetParent === null) continue;
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
      const dialog = document.querySelector('[role="dialog"]');
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

  async function phase3_fillForm() {
    if (!(await isCampaignRunning())) return;

    log("Application form detected — filling fields...", "");
    logBackend("Application form detected — filling fields", "info");
    logFormDiagnostic();

    // Get profile and cover letter
    const storageData = await chrome.storage.local.get([
      "profile",
      "generatedCoverLetter",
      "currentJobInfo",
    ]);
    const profile = storageData.profile || {};
    const coverLetter = storageData.generatedCoverLetter || profile.writing_style || "";
    const jobInfo = storageData.currentJobInfo || {};

    let formStepCount = 0;
    const maxSteps = 20; // Safety: don't loop forever (some jobs have 10+ steps)

    while (formStepCount < maxSteps) {
      if (!(await isCampaignRunning())) {
        log("Campaign stopped — aborting form fill", "");
        return;
      }

      formStepCount++;
      await sleep(humanDelay(1500, 2500));

      // Fill whatever fields are visible on this step
      let filledAny = false;

      // First name
      const fnEl = findFieldBySelectorsOrLabel("firstName");
      if (fnEl && !fnEl.value.trim()) {
        await typeValue(fnEl, profile.name || "");
        await sleep(humanDelay(3000, 5000));
        filledAny = true;
      }

      // Last name
      const lnEl = findFieldBySelectorsOrLabel("lastName");
      if (lnEl && !lnEl.value.trim()) {
        await typeValue(lnEl, profile.last_name || "");
        await sleep(humanDelay(3000, 5000));
        filledAny = true;
      }

      // Email
      const emEl = findFieldBySelectorsOrLabel("email");
      if (emEl && !emEl.value.trim()) {
        await typeValue(emEl, profile.email || "");
        await sleep(humanDelay(3000, 5000));
        filledAny = true;
      }

      // Phone
      const phEl = findFieldBySelectorsOrLabel("phone");
      if (phEl && !phEl.value.trim()) {
        await typeValue(phEl, profile.phone || "");
        await sleep(humanDelay(3000, 5000));
        filledAny = true;
      }

      // Cover letter
      const clEl = findFieldBySelectorsOrLabel("coverLetter");
      if (clEl && !clEl.value.trim()) {
        quickSet(clEl, coverLetter);
        await sleep(humanDelay(3000, 5000));
        filledAny = true;
      }

      // Resume upload
      const resumeInput = findResumeInput();
      if (resumeInput && !resumeInput.files?.length) {
        try {
          await uploadResume(resumeInput);
          await sleep(humanDelay(3000, 5000));
          filledAny = true;
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
        filledAny = true;
      }

      // Required attestation/consent checkboxes (self-attestation, "I certify…").
      const checkboxesFilled = await fillCheckboxes();
      if (checkboxesFilled > 0) {
        await sleep(humanDelay(300, 700));
        filledAny = true;
      }

      // Dropdown screener questions (salary, "how did you hear", demographic/EEO,
      // and custom required <select>s). Without this the form stalls on
      // "Choose an option to continue." Deterministic where possible, AI for the rest.
      const selectsFilled = await fillSelectQuestions();
      if (selectsFilled > 0) {
        await sleep(humanDelay(400, 800));
        filledAny = true;
      }

      // Custom (DIV-based) dropdowns — Indeed's demographic/screener comboboxes.
      const combosFilled = await fillComboboxes();
      if (combosFilled > 0) {
        await sleep(humanDelay(400, 800));
        filledAny = true;
      }

      const textsFilled = await fillTextQuestions();
      if (textsFilled > 0) {
        await sleep(humanDelay(300, 700));
        filledAny = true;
      }

      if (filledAny) {
        log(`Form step ${formStepCount}: filled fields`, "ok");
      }

      // Classify the step's primary button (submit vs continue) by its own text —
      // works on ZipRecruiter's single-step Quick Apply, not just Indeed's modal.
      const action = classifyFormButton();
      if (action.label) log(`Step ${formStepCount} button: "${action.label}" → ${action.submit ? "SUBMIT" : "continue"}`, "");

      // Check if this is the final submit step
      if (action.submit) {
        // Review mode — fill everything but don't submit; report what's filled.
        const reviewMode = (await chrome.storage.local.get("reviewMode")).reviewMode === true;
        if (reviewMode) {
          const nameEl = findFieldBySelectorsOrLabel("firstName") || findFieldBySelectorsOrLabel("fullName");
          const emailEl = findFieldBySelectorsOrLabel("email");
          const resumeEl = findResumeInput();
          log(`REVIEW MODE — filled, NOT submitting: name="${nameEl?.value || ""}" email="${emailEl?.value || ""}" resume=${resumeEl?.files?.length ? resumeEl.files[0].name : "NONE"} radios=${document.querySelectorAll('input[type="radio"]:checked').length}`, "ok");
          logBackend(`📝 Review: ${jobInfo.title} @ ${jobInfo.company} — filled, awaiting your submit`, "info");
          return;
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
        const submitBtn = findFormButton();
        if (submitBtn) {
          // Mark applied + count BEFORE the click: submitting can navigate the whole
          // page (ZipRecruiter returns to results), which would kill this context
          // before an after-the-fact write runs — leaving the job un-marked and
          // re-appliable, and the count unincremented. Recording first is nav-safe.
          await addAppliedUrl(jobInfo.url || window.location.href);
          await addAppliedJobKey(jobInfo.title, jobInfo.company);
          await recordLocalApplication(detectPlatform());

          if (shouldMisclick()) await performMisclick(submitBtn);
          await humanClick(submitBtn);

          // Wait for a real signal the platform accepted the submission.
          // Without this, every Submit click was counted as 'applied' —
          // captcha, error toasts, or silent failures all looked the same.
          const result = await waitForSubmissionConfirmation(20000);

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
          break;
        }
      }

      // Click Continue/Next button to proceed to next step
      const navBtn = action.btn || findFormButton();
      if (navBtn) {
        log(`Clicking "${(navBtn.textContent || "").trim()}"...`, "");
        await sleep(humanDelay(2000, 4000));
        // Re-check after the pause so a Stop mid-step halts before advancing.
        if (!(await isCampaignRunning())) {
          log("Campaign stopped — aborting before next step", "");
          return;
        }
        await humanClick(navBtn);
        // Wait for next step to load
        await sleep(humanDelay(2000, 3000));
      } else {
        // No button found — form might have closed or errored
        log("No Continue/Submit button found", "err");
        break;
      }
    }

    if (formStepCount >= maxSteps) {
      log("Too many form steps — skipping job", "err");
    }

    // If we got here without submitting, skip to next job
    await skipToNextJob();
  }

  async function uploadResume(fileInput) {
    // Resume now lives in Supabase Storage (Phase 3.5). Backend returns a
    // signed URL valid for 1h that the content script fetches directly —
    // the Storage URL doesn't need our Bearer token, the signature is the
    // capability.
    const { currentJobInfo } = await chrome.storage.local.get("currentJobInfo");
    const signed = await sendMsg({ type: "GET_RESUME_URL", jobUrl: currentJobInfo?.url });
    if (!signed?.url) throw new Error(signed?.error || "No resume on server");

    const res = await fetch(signed.url);
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

    const data = await chrome.storage.local.get(["pendingJobs", "currentJobIndex"]);
    const jobs = data.pendingJobs || [];
    const idx = (data.currentJobIndex || 0) + 1;

    if (idx >= jobs.length) {
      // All pending jobs processed — go back to list for next page
      log("All jobs on this page processed", "");
      await goBackToJobList();
      return;
    }

    await chrome.storage.local.set({ currentJobIndex: idx });
    const nextJob = jobs[idx];
    log(`Next job (${idx + 1}/${jobs.length}): ${nextJob.title}`, "");

    await sleep(humanDelay(3000, 5000));

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

  async function goBackToJobList() {
    if (!(await isCampaignRunning())) return;

    const platform = detectPlatform();
    if (platform === "ziprecruiter") return await goBackToZipRecruiterJobList();
    return await goBackToIndeedJobList();
  }

  async function goBackToIndeedJobList() {
    const count = await getPlatformCount("indeed");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`Indeed daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}). Campaign complete.`, "ok");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }

    const data = await chrome.storage.local.get("campaignFilters");
    const filters = data.campaignFilters || {};

    const params = new URLSearchParams();
    if (filters.keywords?.length) params.set("q", filters.keywords.join(" "));
    const locMap = { usa: "United States", remote: "remote", europe: "" };
    const loc = locMap[filters.location] !== undefined ? locMap[filters.location] : (filters.location || "");
    if (loc) params.set("l", loc);
    if (filters.job_type) {
      const jtMap = { "full-time": "fulltime", "part-time": "parttime", contract: "contract" };
      if (jtMap[filters.job_type]) params.set("jt", jtMap[filters.job_type]);
    }
    params.set("iafilter", "1");
    params.set("sort", "date");

    const processed = await chrome.storage.local.get("processedPageStarts");
    const starts = processed.processedPageStarts || [0];
    const lastStart = starts[starts.length - 1];
    const nextStart = lastStart + 10;
    starts.push(nextStart);
    if (starts.length > 200) starts.splice(0, starts.length - 200);
    await chrome.storage.local.set({ processedPageStarts: starts });
    params.set("start", String(nextStart));

    const url = `https://www.indeed.com/jobs?${params.toString()}`;
    log("Returning to job list...", "");
    // 15-30 s between pages — rapid page-flipping triggers Cloudflare rate limiting
    await sleep(humanDelay(15000, 30000));
    window.location.href = url;
  }

  async function goBackToZipRecruiterJobList() {
    const count = await getPlatformCount("ziprecruiter");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`ZipRecruiter daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}). Campaign complete.`, "ok");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }

    const data = await chrome.storage.local.get("campaignFilters");
    const filters = data.campaignFilters || {};

    const params = new URLSearchParams();
    if (filters.keywords?.length) params.set("search", filters.keywords.join(" "));
    const locMap = { usa: "United States", remote: "Remote", europe: "" };
    const loc = locMap[filters.location] !== undefined ? locMap[filters.location] : (filters.location || "");
    if (loc) params.set("location", loc);
    if (filters.job_type) {
      const jtMap = { "full-time": "full_time", "part-time": "part_time", contract: "contract" };
      if (jtMap[filters.job_type]) params.set("employment_type[]", jtMap[filters.job_type]);
    }

    // ZipRecruiter paginates via `page` param (20 jobs per page)
    const processed = await chrome.storage.local.get("processedPageStarts");
    const starts = processed.processedPageStarts || [0];
    const lastStart = starts[starts.length - 1];
    const nextStart = lastStart + 20;
    starts.push(nextStart);
    if (starts.length > 200) starts.splice(0, starts.length - 200);
    await chrome.storage.local.set({ processedPageStarts: starts });
    const page = Math.floor(nextStart / 20) + 1;
    if (page > 1) params.set("page", String(page));

    const url = `https://www.ziprecruiter.com/jobs-search?${params.toString()}`;
    log("Returning to ZipRecruiter job list...", "");
    await sleep(humanDelay(10000, 20000));
    window.location.href = url;
  }

  // Recover when the campaign window lands on a ZipRecruiter page that isn't
  // list/detail/form (e.g. /jobseeker/home after an apply, or a session redirect).
  // Without this the phase is "unknown" forever and the campaign silently stalls.
  // Loop-guarded: if ZR keeps bouncing us off the search, stop with a clear message.
  async function recoverZipRecruiterPhase() {
    const st = await chrome.storage.local.get("zrRecoveries");
    const n = (st.zrRecoveries || 0) + 1;
    if (n > 4) {
      log("ZipRecruiter kept redirecting away from search — stopping campaign", "err");
      logBackend("ZipRecruiter redirect loop — campaign stopped", "error");
      await chrome.storage.local.set({ zrRecoveries: 0 });
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }
    await chrome.storage.local.set({ zrRecoveries: n });

    const data = await chrome.storage.local.get("campaignFilters");
    const filters = data.campaignFilters || {};
    const params = new URLSearchParams();
    if (filters.keywords?.length) params.set("search", filters.keywords.join(" "));
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
  async function phase_ats(platform) {
    if (!(await isCampaignRunning())) return;
    const label = platform === "lever" ? "Lever" : "Greenhouse";

    const count = await getPlatformCount(platform);
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`${label} daily limit reached. Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }

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
    const jobDesc = (descEl?.textContent || "").replace(/\s+/g, " ").trim().slice(0, 1200);
    const jobUrl = window.location.href.split("?")[0];

    if (!jobTitle) { log(`${label}: no job title — skipping`, "err"); return; }

    // Never re-apply (URL or title|company)
    const applied = await getAppliedUrls();
    const appliedJobs = await getAppliedJobKeys();
    if (applied.has(jobUrl) || appliedJobs.has(jobDedupKey(jobTitle, jobCompany))) {
      log(`${jobTitle} — already applied, skipping`, "");
      return;
    }

    log(`${label} job: ${jobTitle} @ ${jobCompany}`, "");

    // Fit gate (M1) — same selective decision layer as the boards
    const fit = await sendMsg({ type: "ASSESS_FIT", data: { job_title: jobTitle, company: jobCompany, description: jobDesc } });
    if (fit && fit.decision === "skip") {
      logBackend(`⏭️ Skipped (fit ${fit.fit_score ?? "?"}): ${jobTitle} @ ${jobCompany} — ${(fit.reason || "").slice(0, 140)}`, "info");
      return;
    }
    if (fit && fit.judged) logBackend(`✓ Good fit (${fit.fit_score}): ${jobTitle} @ ${jobCompany}`, "info");

    // Cover letter
    let coverLetter = "";
    try {
      const cl = await Promise.race([
        sendMsg({ type: "GENERATE_COVER_LETTER", data: { job_title: jobTitle, company: jobCompany, description: jobDesc } }),
        sleep(15000).then(() => ({ error: "timeout" })),
      ]);
      if (cl && cl.letter) coverLetter = cl.letter;
    } catch { /* template fallback */ }
    await chrome.storage.local.set({
      currentJobInfo: { title: jobTitle, company: jobCompany, description: jobDesc, url: jobUrl },
      generatedCoverLetter: coverLetter,
    });

    const profile = (await chrome.storage.local.get("profile")).profile || {};

    log(`${label} — filling application...`, "");
    logBackend("Application form detected — filling fields", "info");
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
    await fillField("email", profile.email || "");
    await fillField("phone", profile.phone || "");

    const resumeInput = findResumeInput();
    if (resumeInput && !resumeInput.files?.length) {
      try { await uploadResume(resumeInput); await sleep(humanDelay(2000, 4000)); }
      catch (e) { log("Resume upload failed: " + e.message, "err"); }
    }

    // Screener questions — reuse the generic answerers (Loop 4 core)
    await fillRadioQuestions();
    await fillTextQuestions();
    await fillSelectQuestions();
    await fillComboboxes();
    await sleep(humanDelay(1500, 2500));

    if (!(await isCampaignRunning())) return;

    const action = classifyFormButton();
    if (action.label) log(`${label} button: "${action.label}" → ${action.submit ? "SUBMIT" : "continue?"}`, "");
    const submitBtn = findFormButton();
    if (!submitBtn) { log(`${label}: submit button not found — skipping`, "err"); return; }

    // Review mode (semi-auto / human-reviews-before-submit): fill everything but do
    // NOT click submit. Report exactly what got filled so the user (or an E2E test)
    // can confirm the form is correct before sending. Nothing is recorded/applied.
    const reviewMode = (await chrome.storage.local.get("reviewMode")).reviewMode === true;
    if (reviewMode) {
      const nameEl = findFieldBySelectorsOrLabel("firstName") || findFieldBySelectorsOrLabel("fullName");
      const emailEl = findFieldBySelectorsOrLabel("email");
      const phoneEl = findFieldBySelectorsOrLabel("phone");
      const resumeEl = findResumeInput();
      const filledTextareas = Array.from(document.querySelectorAll("textarea")).filter((t) => (t.value || "").trim()).length;
      const checkedRadios = document.querySelectorAll('input[type="radio"]:checked').length;
      const summary = `name="${(nameEl?.value || "").slice(0, 40)}" email="${emailEl?.value || ""}" phone="${phoneEl?.value || ""}" resume=${resumeEl?.files?.length ? resumeEl.files[0].name : "NONE"} screener-textareas=${filledTextareas} radios=${checkedRadios} submitBtn="${(submitBtn.textContent || "").replace(/\s+/g, " ").trim().slice(0, 30)}"`;
      log(`REVIEW MODE — filled, NOT submitting: ${summary}`, "ok");
      logBackend(`📝 Review: ${jobTitle} @ ${jobCompany} — filled, awaiting your submit`, "info");
      return; // never submits, never records applied
    }

    await sleep(humanDelay(2000, 5000));
    if (!(await isCampaignRunning())) { log("Campaign stopped — not submitting", ""); return; }

    // Record BEFORE the click — submit navigates to the thank-you page.
    await addAppliedUrl(jobUrl);
    await addAppliedJobKey(jobTitle, jobCompany);
    await recordLocalApplication(platform);
    if (shouldMisclick()) await performMisclick(submitBtn);
    await humanClick(submitBtn);

    const result = await waitForSubmissionConfirmation(20000);
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
    if (platform === "ziprecruiter") return detectPhaseZipRecruiter();
    return detectPhaseIndeed();
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

    // Phase 2: jobs-search page WITH lk= param (job selected, right panel populated)
    try {
      const params = new URL(url).searchParams;
      if (params.get("lk") && (url.includes("/jobs-search") || url.includes("/candidate/search"))) {
        return "detail";
      }
    } catch {}

    // Phase 1: Search results (no lk= param)
    if (url.includes("/jobs-search") || url.includes("/candidate/search") ||
        /ziprecruiter\.com\/?(#.*)?$/.test(url)) return "list";

    return "unknown";
  }

  let _runPhaseActive = false;

  async function runPhase() {
    if (_runPhaseActive) return;
    if (!(await isCampaignRunning())) return;
    _runPhaseActive = true;
    try {
      await _runPhaseInner();
    } finally {
      _runPhaseActive = false;
    }
  }

  async function _runPhaseInner() {
    if (!(await isCampaignRunning())) return;

    // Anti-detect: a CAPTCHA / security challenge is HANDED TO THE USER — we no longer
    // auto-solve it (CapSolver dropped for compliance). The only thing we auto-handle is
    // Cloudflare's passive "Just a moment" JS interstitial, which self-resolves with no
    // user action. Everything else pauses and waits for the human to clear it.
    const det = isDetected();
    if (det.detected) {
      // Cloudflare JS challenge ("Just a moment") — auto-resolves in 3-5s,
      // no user action needed. Wait silently up to 15s before escalating.
      const isCfJsChallenge =
        det.signal === "title:just a moment" ||
        det.signal.includes("cdn-cgi/challenge-platform") ||
        det.signal.includes("cdn-cgi/bm");
      if (isCfJsChallenge) {
        log("Cloudflare check — waiting for auto-resolve...", "");
        for (let i = 0; i < 12; i++) {
          await sleep(5000);
          if (!isDetected().detected) {
            log("Cloudflare resolved — continuing", "ok");
            return;
          }
        }
        // Didn't resolve in 60s — reload tab and try once more before manual pause
        log("Cloudflare didn't resolve in 60s — reloading tab...", "");
        window.location.reload();
        await sleep(15000);
        if (!isDetected().detected) {
          log("Cloudflare resolved after reload — continuing", "ok");
          return;
        }
        // Fall through to the human hand-off if reload also failed.
      }

      // Real challenge (CF managed interstitial, reCAPTCHA / Turnstile checkbox,
      // DataDome…): hand it to the user and STAY PAUSED until they clear it. We no
      // longer force-stop after a few minutes — the user may step away and solve it
      // later; the campaign resumes the moment the page is clean. A generous 2h safety
      // cap avoids an eternal spinner if they never come back.
      log(`⚠️ CAPTCHA — pausing. Solve it in this window; the campaign resumes automatically once it's cleared.`, "err");
      await sendMsg({
        type: "DETECTION_TRIPPED",
        data: { signal: det.signal, url: window.location.href, phase: detectPhase() },
      });
      const _pauseStart = Date.now();
      while (Date.now() - _pauseStart < 2 * 60 * 60 * 1000) {
        await sleep(8000);
        if (!(await isCampaignRunning())) return; // user stopped it themselves
        if (!isDetected().detected) {
          log("CAPTCHA cleared — resuming campaign", "ok");
          break;
        }
      }
      if (isDetected().detected) {
        log("CAPTCHA still not cleared after 2h — stopping campaign", "err");
        await sendMsg({ type: "STOP_CAMPAIGN" });
        return;
      }
    }

    // Login wall: auto-apply is impossible if the user isn't logged into the
    // platform. Detect it, report it (so the dashboard flips to "not connected"),
    // and PAUSE — the user logs in in this same window and we resume. Same pattern
    // as the CAPTCHA hand-off: we never fake-submit against a logged-out session.
    {
      const authPlatform = detectPlatform();
      const authStatus = detectPlatformAuth(authPlatform);
      if (authStatus === "logged_out") {
        await reportPlatformAuth();
        const name = authPlatform === "ziprecruiter" ? "ZipRecruiter" : "Indeed";
        log(`⚠️ Not signed into ${name}. Log in (or create an account) in this window — the campaign resumes automatically once you're in.`, "err");
        await sendMsg({ type: "PLATFORM_LOGIN_REQUIRED", platform: authPlatform, url: window.location.href });
        const _loginPauseStart = Date.now();
        while (Date.now() - _loginPauseStart < 2 * 60 * 60 * 1000) {
          await sleep(8000);
          if (!(await isCampaignRunning())) return; // user stopped it themselves
          if (detectPlatformAuth(authPlatform) === "connected") {
            log(`Signed into ${name} — resuming campaign`, "ok");
            await reportPlatformAuth();
            break;
          }
        }
        if (detectPlatformAuth(authPlatform) === "logged_out") {
          log(`Still not signed into ${name} after 2h — stopping campaign`, "err");
          await sendMsg({ type: "STOP_CAMPAIGN" });
          return;
        }
      }
    }

    const phase = detectPhase();

    // Reaching a known phase means we're on track — clear the ZR recovery counter.
    if (phase !== "unknown") {
      chrome.storage.local.set({ zrRecoveries: 0 }).catch(() => {});
    }

    try {
      switch (phase) {
        case "list":
          await phase1_jobList();
          break;
        case "detail":
          await phase2_jobDetail();
          break;
        case "form": {
          const _p = detectPlatform();
          if (_p === "greenhouse" || _p === "lever") {
            await phase_ats(_p);
          } else {
            await phase3_fillForm();
          }
          break;
        }
        default:
          // Unknown page. On ZipRecruiter this is usually /jobseeker/home or a
          // session redirect — recover to the search instead of stalling forever.
          if (detectPlatform() === "ziprecruiter" && (await isCampaignRunning())) {
            await recoverZipRecruiterPhase();
          }
          break;
      }
    } catch (err) {
      log(`Error in ${phase} phase: ${err.message}`, "err");
      chrome.runtime.sendMessage({
        type: "STEP_FAILED",
        data: { phase, error: err.message },
      });
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

  // =========================================================================
  // Message listener
  // =========================================================================

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    switch (msg.type) {
      case "CAMPAIGN_STARTED":
        log("Campaign started — beginning automation", "ok");
        lastPhase = "";
        runPhase();
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
    // Report whether the user is logged into this platform (for the dashboard's
    // connection status). Runs FIRST — before the selectors fetch — so a slow or
    // failing backend round-trip can never block login detection. Fire-and-forget.
    reportPlatformAuth();

    // Pull DOM selectors from backend (cached 24h) — Phase 4.1
    await loadSelectors();

    // Start observing DOM changes
    if (document.body) {
      observer.observe(document.body, { childList: true, subtree: true });
    }

    // Check if campaign is already running (e.g., page reload)
    if (await isCampaignRunning()) {
      const platformName = detectPlatform() === "ziprecruiter" ? "ZipRecruiter" : "Indeed";
      log("Campaign active — resuming on this page", "ok");
      logBackend(`Extension active on ${platformName} — starting automation`, "info");
      await sleep(humanDelay(2000, 3000));
      // One-shot warmup before the very first action. No-op if already
      // warmed up this campaign.
      await sessionWarmup();
      lastPhase = detectPhase();
      runPhase();
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
  const _screenshotPing = setInterval(() => {
    chrome.runtime.sendMessage({ type: "CAPTURE_SCREENSHOT" }).catch(() => {});
  }, 300);
  window.addEventListener("unload", () => clearInterval(_screenshotPing));
})();
