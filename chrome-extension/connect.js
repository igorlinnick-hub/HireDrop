// HireDrop connection detector — lightweight content script for platforms we can
// CONNECT (account login detection) but don't auto-apply on yet: Glassdoor,
// Wellfound, Monster, CareerBuilder, Dice.
//
// Deliberately separate from content.js (the Indeed/ZipRecruiter automation):
// this script ONLY detects login state and stores it — no campaign logic, no
// selectors fetch, no screenshot pings. Keeping it tiny also keeps the extra
// host permissions defensible for Chrome Web Store review.
//
// Signals verified live 2026-07-10 (logged-out via clean browser; logged-in
// confirmed per-platform as users connect — tighten as we learn):
//   glassdoor    IN:  [data-test="utility-nav-profile-button"]
//                OUT: "Sign In"/"Log In" button text, /member/profile/login link
//   wellfound    OUT: header link to /login
//   monster      OUT: redirect to identity.monster.com (login/registration host)
//   careerbuilder OUT: link with mode=Login / mode=SignUp; also identity.monster.com
//   dice         OUT: redirect to /dashboard/login
// "connected" = the platform's own logged-out markers are ABSENT on a rendered page.

(function () {
  if (window !== window.top) return;
  if (window.__hiredrop_connect_loaded) return;
  window.__hiredrop_connect_loaded = true;

  function detectPlatform() {
    const host = window.location.hostname;
    if (host.includes("glassdoor.")) return "glassdoor";
    if (host.includes("wellfound.com")) return "wellfound";
    // identity.monster.com serves logins for BOTH Monster and CareerBuilder
    // (shared identity since the 2024 merger) — being there proves someone is
    // authenticating, NOT that Monster is logged out. Attributing it to Monster
    // could wrongly downgrade a connected Monster account mid-CB-login. Skip it;
    // detection happens on the destination sites after the redirect back.
    if (host === "identity.monster.com") return null;
    if (host.includes("monster.com")) return "monster";
    if (host.includes("careerbuilder.com")) return "careerbuilder";
    if (host.includes("dice.com")) return "dice";
    return null;
  }

  function hasButtonWithText(re) {
    for (const el of document.querySelectorAll("a, button")) {
      if (re.test((el.textContent || "").trim()) && el.offsetParent !== null) return true;
    }
    return false;
  }

  function pageRendered() {
    return !!document.querySelector("header, nav, [role='navigation'], main");
  }

  function detectAuth(platform) {
    const path = window.location.pathname;

    if (platform === "glassdoor") {
      if (document.querySelector('[data-test="utility-nav-profile-button"], [data-test="mobile-utility-nav-profile-button"]')) return "connected";
      if (document.querySelector('a[href*="member/profile/login"]') || hasButtonWithText(/^(sign in|log in)$/i)) return "logged_out";
      return pageRendered() ? "connected" : "unknown";
    }

    if (platform === "wellfound") {
      if (document.querySelector('a[href*="/logout"], a[href^="/candidate"], a[href*="/jobs/saved"]')) return "connected";
      if (document.querySelector('a[href="/login"], a[href^="/login?"]') || hasButtonWithText(/^log in$/i)) return "logged_out";
      return pageRendered() ? "connected" : "unknown";
    }

    if (platform === "monster") {
      if (hasButtonWithText(/^(log in|sign in)$/i)) return "logged_out";
      return pageRendered() ? "connected" : "unknown";
    }

    if (platform === "careerbuilder") {
      if (document.querySelector('a[href*="mode=Login"], a[href*="mode=SignUp"]') || hasButtonWithText(/^(log in|sign up)$/i)) return "logged_out";
      return pageRendered() ? "connected" : "unknown";
    }

    if (platform === "dice") {
      if (path.startsWith("/dashboard/login") || path.startsWith("/register")) return "logged_out";
      if (hasButtonWithText(/^(log in|sign in)$/i)) return "logged_out";
      return pageRendered() ? "connected" : "unknown";
    }

    return "unknown";
  }

  let lastStored = null; // last status THIS page-load wrote (avoid redundant writes)

  async function store(platform, status) {
    if (status === "unknown" || status === lastStored) return;
    try {
      const s = await chrome.storage.local.get("platformConnections");
      const conns = s.platformConnections || {};
      conns[platform] = { status, checkedAt: new Date().toISOString() };
      await chrome.storage.local.set({ platformConnections: conns });
      lastStored = status;
      chrome.runtime.sendMessage({ type: "PLATFORM_AUTH", platform, status }).catch(() => {});
    } catch { /* extension context gone — ignore */ }
  }

  // Login flows are SPAs / OAuth redirects that often change the page WITHOUT a
  // full reload — a one-shot check at load misses the moment the user signs in
  // (observed live: Wellfound/Monster only flipped after a manual F5). So:
  //   1. poll every 3s for the first 3 minutes after load (login takes a while),
  //   2. after that, re-check on tab focus and on URL changes (cheap, event-driven),
  //   3. write to storage only when the definitive status CHANGES.
  function watch(platform) {
    const ACTIVE_MS = 3 * 60 * 1000;
    const start = Date.now();

    const tick = () => store(platform, detectAuth(platform));

    const interval = setInterval(() => {
      if (Date.now() - start > ACTIVE_MS) { clearInterval(interval); return; }
      tick();
    }, 3000);

    document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

    let lastUrl = window.location.href;
    setInterval(() => {
      if (window.location.href !== lastUrl) {
        lastUrl = window.location.href;
        // Give the new view a moment to render, then check twice.
        setTimeout(tick, 1500);
        setTimeout(tick, 4000);
      }
    }, 1000);

    tick();
  }

  function init() {
    const platform = detectPlatform();
    if (!platform) return;
    watch(platform);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    setTimeout(init, 500);
  }
})();
