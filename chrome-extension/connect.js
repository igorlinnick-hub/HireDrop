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
    if (host.includes("monster.com")) return "monster"; // includes identity.monster.com
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
    const host = window.location.hostname;
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
      // The identity host IS the login/registration flow — being here means logged out.
      if (host.includes("identity.monster.com")) return "logged_out";
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

  async function report() {
    const platform = detectPlatform();
    if (!platform) return;

    // The nav/header can render after document_idle — poll for a definitive answer.
    let status = detectAuth(platform);
    for (let i = 0; i < 8 && status === "unknown"; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      status = detectAuth(platform);
    }
    if (status === "unknown") return;

    try {
      const store = await chrome.storage.local.get("platformConnections");
      const conns = store.platformConnections || {};
      // Monster and CareerBuilder share one identity account (merged 2024) — but only
      // propagate the POSITIVE signal. Being logged out of one page doesn't prove the
      // other is (separate cookies per domain).
      conns[platform] = { status, checkedAt: new Date().toISOString() };
      await chrome.storage.local.set({ platformConnections: conns });
      chrome.runtime.sendMessage({ type: "PLATFORM_AUTH", platform, status }).catch(() => {});
    } catch { /* extension context gone — ignore */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", report);
  } else {
    setTimeout(report, 500);
  }
})();
