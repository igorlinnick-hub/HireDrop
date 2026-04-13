// JobFlow content script — Indeed.com auto-apply automation
// Injected on indeed.com pages via manifest content_scripts
//
// Three phases:
//   PHASE 1 — Job list page (/jobs?): scan for "Easily apply" cards, click first
//   PHASE 2 — Job detail page (/viewjob): extract info, generate cover letter, click Apply
//   PHASE 3 — Application form: fill fields, click through steps, submit

(function () {
  if (window.__jobflow_loaded) return;
  window.__jobflow_loaded = true;

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

  async function isCampaignRunning() {
    const data = await chrome.storage.local.get("campaignRunning");
    return !!data.campaignRunning;
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
    await sleep(rand(100, 200));

    // Clear existing value first
    setNativeValue(el, "");
    await sleep(rand(50, 100));

    // Type each character
    for (let i = 0; i < value.length; i++) {
      const partial = value.slice(0, i + 1);
      setNativeValue(el, partial);
      await sleep(rand(50, 150));
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

  const JOB_CARD_SELECTORS = [
    ".job_seen_beacon",
    ".resultContent",
    ".jobsearch-ResultsList li",
    "[data-jk]",
    'div[class*="cardOutline"]',
    'td.resultContent',
  ];

  function findJobCards() {
    for (const sel of JOB_CARD_SELECTORS) {
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
    const jk = card.getAttribute("data-jk") || titleEl?.getAttribute("data-jk") || "";

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
    await sleep(rand(2000, 3000));

    const cards = findJobCards();
    if (!cards.length) {
      log("No job cards found on page", "err");
      return;
    }

    // Filter for "Easily apply" jobs
    const easyApplyCards = [];
    const alreadyApplied = await getAppliedUrls();

    for (const card of cards) {
      if (!isEasilyApplyCard(card)) continue;
      const info = extractCardInfo(card);
      if (!info.title || !info.clickEl) continue;
      if (alreadyApplied.has(info.url)) continue;
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

    // Click the first job card
    const firstJob = easyApplyCards[0];
    log(`Opening: ${firstJob.title} @ ${firstJob.company}`, "");
    await sleep(rand(2000, 3000));
    firstJob.clickEl.click();
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
      await sleep(rand(2000, 3000));
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
    await sleep(rand(1500, 2500));

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

    // Check if it's an Easy Apply job
    const pageText = document.body.textContent || "";
    if (!/easily\s*apply/i.test(pageText)) {
      log(`${jobTitle} — not Easy Apply, skipping`, "");
      await skipToNextJob();
      return;
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

    // Find and click the Apply button
    await sleep(rand(2000, 3000));

    const applyBtn = findApplyButton();
    if (!applyBtn) {
      log("No Apply button found — skipping", "err");
      await skipToNextJob();
      return;
    }

    log("Clicking Apply button...", "");
    applyBtn.click();

    // Phase 3 will be triggered by MutationObserver detecting the form
  }

  function findApplyButton() {
    // Indeed's apply buttons in priority order
    const selectors = [
      'button[id*="indeedApply"]',
      ".ia-IndeedApplyButton",
      'button[class*="IndeedApply"]',
      'button[aria-label*="Apply now"]',
      'a[href*="/applystart"]',
      'button[data-testid*="apply"]',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return el;
    }

    // Fallback: find button containing "Apply" text
    const buttons = document.querySelectorAll("button, a");
    for (const btn of buttons) {
      const text = btn.textContent?.trim() || "";
      if (/^(apply now|easily apply|apply)$/i.test(text) && btn.offsetParent !== null) {
        return btn;
      }
    }
    return null;
  }

  // =========================================================================
  // PHASE 3 — Application Form (multi-step)
  // =========================================================================

  const FIELD_SELECTORS = {
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
  };

  const LABEL_FALLBACKS = {
    firstName: "first name",
    lastName: "last name",
    email: "email",
    phone: "phone",
    coverLetter: "cover letter",
  };

  function findFieldBySelectorsOrLabel(fieldName) {
    // Try direct selectors first
    const selectors = FIELD_SELECTORS[fieldName] || [];
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
      await sleep(rand(1500, 2500));

      // Fill whatever fields are visible on this step
      let filledAny = false;

      // First name
      const fnEl = findFieldBySelectorsOrLabel("firstName");
      if (fnEl && !fnEl.value.trim()) {
        await typeValue(fnEl, profile.name || "");
        await sleep(rand(3000, 5000));
        filledAny = true;
      }

      // Last name
      const lnEl = findFieldBySelectorsOrLabel("lastName");
      if (lnEl && !lnEl.value.trim()) {
        await typeValue(lnEl, profile.last_name || "");
        await sleep(rand(3000, 5000));
        filledAny = true;
      }

      // Email
      const emEl = findFieldBySelectorsOrLabel("email");
      if (emEl && !emEl.value.trim()) {
        await typeValue(emEl, profile.email || "");
        await sleep(rand(3000, 5000));
        filledAny = true;
      }

      // Phone
      const phEl = findFieldBySelectorsOrLabel("phone");
      if (phEl && !phEl.value.trim()) {
        await typeValue(phEl, profile.phone || "");
        await sleep(rand(3000, 5000));
        filledAny = true;
      }

      // Cover letter
      const clEl = findFieldBySelectorsOrLabel("coverLetter");
      if (clEl && !clEl.value.trim()) {
        quickSet(clEl, coverLetter);
        await sleep(rand(3000, 5000));
        filledAny = true;
      }

      // Resume upload
      const resumeInput = findResumeInput();
      if (resumeInput && !resumeInput.files?.length) {
        try {
          await uploadResume(resumeInput);
          await sleep(rand(3000, 5000));
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
        await sleep(rand(2000, 4000));
        const submitBtn = findFormButton();
        if (submitBtn) {
          submitBtn.click();
          log(`Applied: ${jobInfo.title} @ ${jobInfo.company}`, "ok");

          // Report to background
          await sendMsg({
            type: "APPLICATION_SAVED",
            data: {
              job_title: jobInfo.title || "",
              company: jobInfo.company || "",
              platform: "indeed",
              job_url: jobInfo.url || window.location.href,
              cover_letter: coverLetter,
              status: "applied",
            },
          });

          await addAppliedUrl(jobInfo.url || window.location.href);

          // Wait then go back to job list
          await sleep(rand(4000, 6000));
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
        await sleep(rand(2000, 4000));
        navBtn.click();
        // Wait for next step to load
        await sleep(rand(2000, 3000));
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
    // Fetch resume PDF from API
    const res = await fetch(`${API_BASE}/api/resume-download`);
    if (!res.ok) throw new Error("No resume on server");

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

    await sleep(rand(3000, 5000));

    // Navigate to the job
    if (nextJob.url) {
      window.location.href = nextJob.url;
    }
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
    await sleep(rand(3000, 5000));
    window.location.href = url;
  }

  // =========================================================================
  // Phase detection & routing
  // =========================================================================

  function detectPhase() {
    const url = window.location.href;

    // Phase 3: Indeed apply form is visible (modal or full page)
    if (isFormVisible()) return "form";

    // Phase 2: Viewing a specific job
    if (url.includes("/viewjob") || url.includes("vjk=")) return "detail";

    // Phase 1: Job search results list
    if (url.includes("/jobs?") || url.includes("/jobs#")) return "list";

    // Also handle the job detail panel on the search results page
    // (Indeed sometimes shows job details in a right panel)
    const detailPanel = document.querySelector("#jobsearch-ViewjobPaneWrapper");
    if (detailPanel && detailPanel.offsetParent !== null) return "detail";

    return "unknown";
  }

  async function runPhase() {
    if (!(await isCampaignRunning())) return;

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
      await sleep(rand(3000, 5000));
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
    // Start observing DOM changes
    if (document.body) {
      observer.observe(document.body, { childList: true, subtree: true });
    }

    // Check if campaign is already running (e.g., page reload)
    if (await isCampaignRunning()) {
      log("Campaign active — resuming on this page", "ok");
      await sleep(rand(2000, 3000));
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
})();
