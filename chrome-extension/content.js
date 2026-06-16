// HireDrop content script — Indeed.com auto-apply automation
// Injected on indeed.com pages via manifest content_scripts
//
// Three phases:
//   PHASE 1 — Job list page (/jobs?): scan for "Easily apply" cards, click first
//   PHASE 2 — Job detail page (/viewjob): extract info, generate cover letter, click Apply
//   PHASE 3 — Application form: fill fields, click through steps, submit

(function () {
  if (window.__hiredrop_loaded) return;
  window.__hiredrop_loaded = true;

  const MAX_APPLICATIONS_PER_PLATFORM = 50;

  // =========================================================================
  // Utilities
  // =========================================================================

  function sendMsg(msg) {
    return new Promise((resolve) =>
      chrome.runtime.sendMessage(msg, (res) => resolve(res))
    );
  }

  function log(text, cls) {
    chrome.runtime.sendMessage({ type: "LOG", text, cls: cls || "" });
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
    log(`Warmup complete (${Math.round(elapsed / 1000)}s)`, "ok");
    await chrome.storage.local.set({ campaignWarmedUp: true });
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
  async function waitForSubmissionConfirmation(timeoutMs = 8000) {
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
    ];
    const POSTAPPLY_URL_HINTS = ["/applied", "postapply", "post_apply", "thank-you", "thankyou"];

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
        'input[autocomplete="given-name"]',
      ],
      lastName: [
        'input[name*="lastName" i]',
        'input[id*="lastName" i]',
        'input[name*="last_name" i]',
        'input[autocomplete="family-name"]',
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
      // Cloudflare / DataDome / etc.
      "#challenge-form",
      "#challenge-running",
      "[data-cf-challenge]",
      'div[class*="captcha-container"]',
      '[id*="datadome"]',
      'script[src*="cdn-cgi/challenge-platform"]',
      // Indeed-specific: visible "verify you are human" overlay
      '[data-testid="captcha-modal"]',
      '[class*="indeed-captcha"]',
    ],
    // Match against any script src — separate from domSelectors because
    // <script> nodes are never offsetParent-visible.
    scriptSrcPatterns: [
      "cdn-cgi/challenge-platform",
      "cdn-cgi/bm/cv",
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
    try {
      const cached = await chrome.storage.local.get(["selectors_indeed", "selectors_indeed_at"]);
      const fresh = cached.selectors_indeed_at && Date.now() - cached.selectors_indeed_at < 24 * 3600 * 1000;
      if (fresh && cached.selectors_indeed) {
        SELECTORS = cached.selectors_indeed;
        return;
      }
      const resp = await sendMsg({ type: "GET_SELECTORS", platform: "indeed" });
      if (resp?.selectors) {
        SELECTORS = resp.selectors;
        await chrome.storage.local.set({
          selectors_indeed: resp.selectors,
          selectors_indeed_at: Date.now(),
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
      const m = href.match(/[?&]jk=([a-f0-9]+)/i);
      if (m) jk = m[1];
    }

    return { title, company, url, jk, clickEl: titleEl };
  }

  async function phase1_jobList() {
    if (!(await isCampaignRunning())) return;

    const count = await getPlatformCount("indeed");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`Indeed daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}). Stopping.`, "");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }

    log("Scanning job list for Easy Apply jobs...", "");

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
      await goToNextPage();
      return;
    }

    log(`Found ${easyApplyCards.length} Easy Apply jobs`, "ok");

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

  async function goToNextPage() {
    const nextBtn =
      document.querySelector('a[data-testid="pagination-page-next"]') ||
      document.querySelector('a[aria-label="Next Page"]') ||
      document.querySelector('nav a[aria-label*="Next"]') ||
      document.querySelector('.pagination a:last-child');

    if (nextBtn) {
      log("Going to next page...", "");
      await sleep(humanDelay(2000, 3000));
      nextBtn.click();
    } else {
      log("No more pages. Campaign complete.", "ok");
      await sendMsg({ type: "STOP_CAMPAIGN" });
    }
  }

  // =========================================================================
  // PHASE 2 — Job Detail / View Job
  // =========================================================================

  async function phase2_jobDetail() {
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

    // Deduplicate by job key (vjk= in URL) to avoid reprocessing the same job
    const jkMatch = jobUrl.match(/[?&](?:vjk|jk)=([a-f0-9]+)/i);
    const jobKey = jkMatch ? jkMatch[1] : null;
    if (jobKey) {
      const seen = await chrome.storage.local.get("processedJobKeys");
      const keys = seen.processedJobKeys || [];
      if (keys.includes(jobKey)) {
        log(`${jobTitle} — already processed, skipping`, "");
        await skipToNextJob();
        return;
      }
      await chrome.storage.local.set({ processedJobKeys: [...keys, jobKey].slice(-500) });
    }

    // Quick check: if only "Apply on company site" is visible, skip before cover letter
    const hasIndeedApply = !!findApplyButton();
    const hasExternalOnly = !hasIndeedApply && !!document.querySelector('button[aria-label*="company site" i], a[aria-label*="company site" i]');
    if (hasExternalOnly) {
      log(`${jobTitle} — external apply only, skipping`, "");
      await skipToNextJob();
      return;
    }

    // Check if it's an Easy Apply job (use the apply button presence, not page text which matches filter chip)
    if (!hasIndeedApply) {
      // Give it 3 seconds for async load before giving up
      await sleep(3000);
      if (!findApplyButton()) {
        log(`${jobTitle} — no Easy Apply button found, skipping`, "");
        await skipToNextJob();
        return;
      }
    }

    log(`Job: ${jobTitle} @ ${jobCompany}`, "");

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
      log("No Apply button found — skipping", "err");
      await skipToNextJob();
      return;
    }

    log("Clicking Apply button...", "");
    if (shouldMisclick()) await performMisclick(applyBtn);
    await humanClick(applyBtn);

    // Phase 3 will be triggered by MutationObserver detecting the form
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

  function findFormButton() {
    // Look for form navigation/submit buttons
    const selectors = [
      'button[type="submit"]',
      "button.ia-continueButton",
      'button[data-testid="continue-button"]',
      'button[data-testid="submit-button"]',
      'button[aria-label*="Continue"]',
      'button[aria-label*="Submit"]',
      'button[aria-label*="Review"]',
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
    const buttons = document.querySelectorAll("button");
    for (const btn of buttons) {
      const text = btn.textContent?.trim().toLowerCase() || "";
      if (
        text.includes("submit application") ||
        text.includes("submit your application") ||
        text === "submit"
      ) {
        return true;
      }
    }
    return false;
  }

  function isFormVisible() {
    // Check if an Indeed apply form/modal is visible
    const indicators = [
      ".ia-BasePage",
      ".ia-InterviewPage",
      '[class*="ia-"]',
      'form[action*="apply"]',
      '[data-testid="apply-form"]',
      ".icl-Modal",
      '[role="dialog"]',
    ];
    for (const sel of indicators) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return true;
    }
    return false;
  }

  function findResumeInput() {
    // File inputs for resume upload
    const inputs = document.querySelectorAll('input[type="file"]');
    for (const input of inputs) {
      const parent = input.closest("div, label, fieldset");
      const text = parent?.textContent?.toLowerCase() || "";
      if (text.includes("resume") || text.includes("cv")) return input;
    }
    // Any file input as fallback
    if (inputs.length === 1) return inputs[0];
    return null;
  }

  async function phase3_fillForm() {
    if (!(await isCampaignRunning())) return;

    log("Application form detected — filling fields...", "");

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
    const maxSteps = 8; // Safety: don't loop forever

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

      if (filledAny) {
        log(`Form step ${formStepCount}: filled fields`, "ok");
      }

      // Check if this is the final submit step
      if (isSubmitStep()) {
        log("Final step — submitting application...", "");
        // Last-look pause is longer than mid-form steps — real users
        // re-read the summary before committing.
        await sleep(humanDelay(3000, 8000));
        const submitBtn = findFormButton();
        if (submitBtn) {
          if (shouldMisclick()) await performMisclick(submitBtn);
          await humanClick(submitBtn);

          // Wait for a real signal that Indeed accepted the submission.
          // Without this, every Submit click was counted as 'applied' —
          // captcha, error toasts, or silent failures all looked the same.
          const result = await waitForSubmissionConfirmation(8000);

          if (result.verified) {
            log(`Applied (verified ${result.signal}): ${jobInfo.title} @ ${jobInfo.company}`, "ok");
            await sendMsg({
              type: "APPLICATION_SAVED",
              data: {
                job_title: jobInfo.title || "",
                company: jobInfo.company || "",
                platform: "indeed",
                job_url: jobInfo.url || window.location.href,
                cover_letter: coverLetter,
                status: "applied",
                verified: true,
                verify_signal: result.signal,
              },
            });
            await addAppliedUrl(jobInfo.url || window.location.href);
          } else {
            // Submit clicked but no confirmation — do NOT increment counter,
            // do NOT add to applied URLs (so we can retry on next pass).
            log(`Submit unverified for ${jobInfo.title} @ ${jobInfo.company} (${result.signal})`, "err");
            await sendMsg({
              type: "STEP_FAILED",
              data: {
                phase: "verify_submission",
                reason: "submit_unverified",
                job_title: jobInfo.title || "",
                company: jobInfo.company || "",
                job_url: jobInfo.url || window.location.href,
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
      const navBtn = findFormButton();
      if (navBtn) {
        log(`Clicking "${navBtn.textContent.trim()}"...`, "");
        await sleep(humanDelay(2000, 4000));
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
    const signed = await sendMsg({ type: "GET_RESUME_URL" });
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

    // Use /viewjob?jk= directly — card hrefs (/rc/clk?...) may redirect back to
    // /jobs?...&vjk= which detectPhase() now treats as "list", re-running phase1.
    const targetUrl = nextJob.jk
      ? `https://www.indeed.com/viewjob?jk=${nextJob.jk}`
      : nextJob.url;
    window.location.href = targetUrl;
  }

  async function goBackToJobList() {
    if (!(await isCampaignRunning())) return;

    const count = await getPlatformCount("indeed");
    if (count >= MAX_APPLICATIONS_PER_PLATFORM) {
      log(`Indeed daily limit reached (${count}/${MAX_APPLICATIONS_PER_PLATFORM}). Campaign complete.`, "ok");
      await sendMsg({ type: "STOP_CAMPAIGN" });
      return;
    }

    // Navigate back to the search results
    // Try to find the search URL from the referrer or build from filters
    const data = await chrome.storage.local.get("campaignFilters");
    const filters = data.campaignFilters || {};

    // Build search URL
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

    // Increment start to skip already-seen jobs
    const processed = await chrome.storage.local.get("processedPageStarts");
    const starts = processed.processedPageStarts || [0];
    const lastStart = starts[starts.length - 1];
    const nextStart = lastStart + 10;
    starts.push(nextStart);
    await chrome.storage.local.set({ processedPageStarts: starts });
    params.set("start", String(nextStart));

    const url = `https://www.indeed.com/jobs?${params.toString()}`;
    log("Returning to job list...", "");
    // 15-30 s between pages — rapid page-flipping triggers Cloudflare rate limiting
    await sleep(humanDelay(15000, 30000));
    window.location.href = url;
  }

  // =========================================================================
  // Phase detection & routing
  // =========================================================================

  function detectPhase() {
    const url = window.location.href;

    // Phase 3: Indeed apply form is visible (modal or full page)
    if (isFormVisible()) return "form";

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

  async function runPhase() {
    if (!(await isCampaignRunning())) return;

    // Anti-detect safety net (Phase 5.5). If Indeed shows a real CAPTCHA or
    // security challenge, pause and ask the user to solve it manually.
    const det = isDetected();
    if (det.detected) {
      log(`⚠️ CAPTCHA detected (${det.signal}) — pausing. Solve it in this window, then the campaign will resume.`, "err");
      await sendMsg({
        type: "DETECTION_TRIPPED",
        data: { signal: det.signal, url: window.location.href, phase: detectPhase() },
      });
      // Wait up to 3 minutes for the user to solve the CAPTCHA, then retry
      for (let i = 0; i < 36; i++) {
        await sleep(5000);
        if (!(await isCampaignRunning())) return;
        const recheck = isDetected();
        if (!recheck.detected) {
          log("CAPTCHA resolved — resuming campaign", "ok");
          break;
        }
        if (i === 35) {
          log("CAPTCHA not solved in 3 min — stopping campaign", "err");
          await sendMsg({ type: "STOP_CAMPAIGN" });
          return;
        }
      }
    }

    const phase = detectPhase();

    try {
      switch (phase) {
        case "list":
          await phase1_jobList();
          break;
        case "detail":
          await phase2_jobDetail();
          break;
        case "form":
          await phase3_fillForm();
          break;
        default:
          // Unknown page — wait and re-check
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
    // Pull DOM selectors from backend (cached 24h) — Phase 4.1
    await loadSelectors();

    // Start observing DOM changes
    if (document.body) {
      observer.observe(document.body, { childList: true, subtree: true });
    }

    // Check if campaign is already running (e.g., page reload)
    if (await isCampaignRunning()) {
      log("Campaign active — resuming on this page", "ok");
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
  }, 2500);
  window.addEventListener("unload", () => clearInterval(_screenshotPing));
})();
