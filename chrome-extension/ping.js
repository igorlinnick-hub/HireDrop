// HireDrop extension <-> dashboard bridge.
// Runs as a content script on the HireDrop website. Forwards two kinds of
// page-originated messages to the background service worker:
//   HIREDROP_PING          → respond with HIREDROP_PONG (extension detection)
//   HIREDROP_STORE_TOKEN   → STORE_TOKEN to background, then HIREDROP_TOKEN_STORED back
//   HIREDROP_READ_STORAGE  → read chrome.storage.local keys, post HIREDROP_STORAGE_DATA back (debug)
//   HIREDROP_TEST_ARM_ATS  → (test-only, review-mode-gated) set campaignRunning so an open
//                            Greenhouse/Lever tab runs phase_ats without a real campaign
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data) return;

  if (e.data === "HIREDROP_PING") {
    window.postMessage("HIREDROP_PONG", "*");
    return;
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_STORE_TOKEN" && typeof e.data.token === "string") {
    window.__hd_store_attempt = Date.now();
    try {
      chrome.runtime.sendMessage(
        { type: "STORE_TOKEN", token: e.data.token, refresh_token: e.data.refresh_token || "" },
        function (resp) {
          const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
          const ok = !!(resp && resp.stored);
          window.__hd_store_result = { ok, error: err, ping_status: resp && resp.ping_status, ts: Date.now() };
          window.postMessage(
            { type: "HIREDROP_TOKEN_STORED", ok, error: err, ping_status: resp && resp.ping_status },
            "*"
          );
        }
      );
    } catch (ex) {
      // Extension context invalidated — content script is orphaned (extension was reloaded
      // after this page was opened). Signal the page so it can auto-reload and get a fresh
      // content script injection.
      window.postMessage(
        { type: "HIREDROP_TOKEN_STORED", ok: false, error: "context_invalidated" },
        "*"
      );
    }
  }

  // Durable extension API key (Approach A). Dashboard mints it and hands it here.
  if (typeof e.data === "object" && e.data.type === "HIREDROP_STORE_KEY" && typeof e.data.key === "string") {
    try {
      chrome.runtime.sendMessage(
        { type: "STORE_KEY", key: e.data.key },
        function (resp) {
          const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
          window.postMessage(
            { type: "HIREDROP_KEY_STORED", ok: !!(resp && resp.stored), error: err, ping_status: resp && resp.ping_status },
            "*"
          );
        }
      );
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_KEY_STORED", ok: false, error: "context_invalidated" }, "*");
    }
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_READ_STORAGE") {
    const keys = e.data.keys || ["supabase_token", "supabase_refresh_token", "profile"];
    chrome.storage.local.get(keys, function (data) {
      const redacted = {};
      for (const k of keys) {
        const v = data[k];
        if (typeof v === "string" && v.length > 20) {
          redacted[k] = v.slice(0, 12) + "...(len=" + v.length + ")";
        } else {
          redacted[k] = v;
        }
      }
      window.postMessage({ type: "HIREDROP_STORAGE_DATA", data: redacted }, "*");
    });
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_START_CAMPAIGN") {
    chrome.runtime.sendMessage(
      { type: "START_CAMPAIGN", filters: e.data.filters || {} },
      function (resp) {
        window.postMessage(
          {
            type: "HIREDROP_CAMPAIGN_STARTED",
            ok: !!(resp && resp.started),
            error: resp && resp.error,
            message: resp && resp.message,
            platform: resp && resp.platform,
          },
          "*"
        );
      }
    );
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_STOP_CAMPAIGN") {
    chrome.runtime.sendMessage({ type: "STOP_CAMPAIGN" }, function () {});
  }

  // Toggle review mode: fill applications but stop before Submit (semi-auto / the
  // user reviews and submits). Also used for safe E2E testing (no real applications).
  if (typeof e.data === "object" && e.data.type === "HIREDROP_SET_REVIEW") {
    try {
      chrome.storage.local.set({ reviewMode: e.data.on === true }, function () {
        window.postMessage({ type: "HIREDROP_REVIEW_SET", on: e.data.on === true }, "*");
      });
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_REVIEW_SET", on: false, error: "context_invalidated" }, "*");
    }
  }

  // TEST-ONLY: flip campaignRunning so an already-open Greenhouse/Lever tab exercises
  // phase_ats WITHOUT starting a real ZR/Indeed campaign (no window, no submissions).
  // DOUBLE-GATED so it is dead in production: (1) requires chrome.storage.local.hd_debug
  // === true (no prod user sets this), and (2) requires reviewMode === true so it can
  // NEVER cause a real application — only the fill-but-don't-submit path. For safe E2E
  // of the ATS fillers. To enable during dev: set hd_debug + reviewMode in storage first.
  if (typeof e.data === "object" && e.data.type === "HIREDROP_TEST_ARM_ATS") {
    try {
      chrome.storage.local.get(["reviewMode", "hd_debug"], function (d) {
        const armed = d.hd_debug === true && d.reviewMode === true;
        if (armed) chrome.storage.local.set({ campaignRunning: true });
        const reason = d.hd_debug !== true ? "hd_debug flag off (prod-safe)"
          : d.reviewMode !== true ? "reviewMode must be ON first" : null;
        window.postMessage({ type: "HIREDROP_TEST_ARMED", armed, reason }, "*");
      });
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_TEST_ARMED", armed: false, error: "context_invalidated" }, "*");
    }
  }

  // Seed the cross-run dedup set with title|company keys (lowercased, single-spaced).
  // Used to pre-mark jobs already applied to before the dedup existed, so they're
  // never re-applied. Writes chrome.storage.local directly (page context can't).
  if (typeof e.data === "object" && e.data.type === "HIREDROP_MARK_APPLIED" && Array.isArray(e.data.keys)) {
    try {
      chrome.storage.local.get("appliedJobKeys", function (d) {
        const keys = d.appliedJobKeys || [];
        for (const k of e.data.keys) { if (k && keys.indexOf(k) === -1) keys.push(k); }
        chrome.storage.local.set({ appliedJobKeys: keys }, function () {
          window.postMessage({ type: "HIREDROP_MARKED_APPLIED", count: keys.length }, "*");
        });
      });
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_MARKED_APPLIED", count: -1, error: "context_invalidated" }, "*");
    }
  }

  // Platform account connection status (Indeed / ZipRecruiter login state).
  // Read chrome.storage.local DIRECTLY here — content.js writes it directly too,
  // so there's no need to round-trip through the service worker. This is also more
  // robust: an MV3 service worker can go stale (kept alive on old code by screenshot
  // pings), which made a background round-trip return empty even though storage was
  // populated. The content-script context always sees fresh storage.
  if (typeof e.data === "object" && e.data.type === "HIREDROP_GET_PLATFORM_CONNECTIONS") {
    try {
      chrome.storage.local.get("platformConnections", function (data) {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
        window.postMessage(
          { type: "HIREDROP_PLATFORM_CONNECTIONS", ok: !err, connections: (data && data.platformConnections) || {}, error: err },
          "*"
        );
      });
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_PLATFORM_CONNECTIONS", ok: false, connections: {}, error: "context_invalidated" }, "*");
    }
  }

  // Open a platform's login / sign-up page so the user can connect (or register).
  if (typeof e.data === "object" && e.data.type === "HIREDROP_OPEN_PLATFORM_LOGIN" && typeof e.data.platform === "string") {
    try {
      chrome.runtime.sendMessage({ type: "OPEN_PLATFORM_LOGIN", platform: e.data.platform }, function (resp) {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
        window.postMessage(
          { type: "HIREDROP_PLATFORM_LOGIN_OPENED", ok: !!(resp && resp.ok), platform: e.data.platform, error: err },
          "*"
        );
      });
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_PLATFORM_LOGIN_OPENED", ok: false, platform: e.data.platform, error: "context_invalidated" }, "*");
    }
  }
});
