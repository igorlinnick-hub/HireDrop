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
    try {
      chrome.runtime.sendMessage(
        { type: "START_CAMPAIGN", filters: e.data.filters || {} },
        function (resp) {
          // No receiver (service worker dead / message dropped) → lastError set,
          // resp undefined. Report it as a stale context so the dashboard can prompt
          // a reload instead of the user staring at an idle screen forever.
          if (chrome.runtime.lastError || !resp) {
            window.postMessage({ type: "HIREDROP_CAMPAIGN_STARTED", ok: false, error: "context_invalidated" }, "*");
            return;
          }
          window.postMessage(
            {
              type: "HIREDROP_CAMPAIGN_STARTED",
              ok: !!resp.started,
              error: resp.error,
              message: resp.message,
              platform: resp.platform,
            },
            "*"
          );
        }
      );
    } catch (ex) {
      // Synchronous throw = "Extension context invalidated" (extension was reloaded
      // but this tab wasn't). Only a page reload reconnects the content script.
      window.postMessage({ type: "HIREDROP_CAMPAIGN_STARTED", ok: false, error: "context_invalidated" }, "*");
    }
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_STOP_CAMPAIGN") {
    try { chrome.runtime.sendMessage({ type: "STOP_CAMPAIGN" }, function () { void chrome.runtime.lastError; }); }
    catch (ex) { /* stale context — the page reload that fixes Start fixes this too */ }
  }

  // Dev self-reload: reload the unpacked extension without a manual chrome://extensions click,
  // so live-testing iterates fast. Safe: ping.js runs ONLY on hiredrop.io (manifest), so only
  // our own dashboard can trigger it, and chrome.runtime.reload() carries no data risk.
  if (typeof e.data === "object" && e.data.type === "HIREDROP_DEV_RELOAD") {
    try { chrome.runtime.sendMessage({ type: "DEV_RELOAD" }, function () { void chrome.runtime.lastError; }); }
    catch (ex) { /* stale context */ }
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

  // Runtime feature flags, WHITELISTED (not a generic storage-write hole). Used by the
  // all-platform tap rollout: tapNativePool gates whether swiped Indeed/ZR jobs enter
  // the pool apply queue (see background.js normalizePoolApplyUrl). Boolean-only.
  if (typeof e.data === "object" && e.data.type === "HIREDROP_SET_FLAG") {
    const ALLOWED_FLAGS = ["tapNativePool", "hd_debug"];
    if (ALLOWED_FLAGS.indexOf(e.data.key) !== -1) {
      try {
        const val = e.data.value === true;
        const upd = {};
        upd[e.data.key] = val;
        chrome.storage.local.set(upd, function () {
          window.postMessage({ type: "HIREDROP_FLAG_SET", key: e.data.key, value: val }, "*");
        });
      } catch (ex) {
        window.postMessage({ type: "HIREDROP_FLAG_SET", key: e.data.key, error: "context_invalidated" }, "*");
      }
    }
  }

  // DEV-ONLY: relay a self-reload request (background double-checks hd_debug).
  if (typeof e.data === "object" && e.data.type === "HIREDROP_RELOAD_EXT") {
    try {
      chrome.runtime.sendMessage({ type: "RELOAD_SELF" }, function (resp) {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
        window.postMessage({ type: "HIREDROP_RELOAD_ACK", ok: !!(resp && resp.reloading), error: err || (resp && resp.error) }, "*");
      });
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_RELOAD_ACK", ok: false, error: "context_invalidated" }, "*");
    }
  }

  // Tap-mode verdict from the dashboard review card: the human tapped Approve or Skip.
  // content.js (in the automation window) polls chrome.storage.reviewDecision and, when
  // the id matches the pending review, submits (approve) or skips. This is the return
  // leg of the reviewPending bridge — decision must carry the same id.
  if (typeof e.data === "object" && e.data.type === "HIREDROP_REVIEW_DECISION" && e.data.id) {
    try {
      const decision = e.data.decision === "approve" ? "approve" : "skip";
      // Also clear reviewPending immediately so the card leaves the dashboard the moment
      // you tap — even if no live content.js loop is around to consume the decision (a
      // stale card from a stopped run). A live loop polls reviewDecision, not
      // reviewPending, so clearing it here doesn't stop it from submitting/skipping.
      chrome.storage.local.set(
        { reviewDecision: { id: e.data.id, decision: decision, at: Date.now() }, reviewPending: null },
        function () {
          window.postMessage({ type: "HIREDROP_REVIEW_DECISION_SET", id: e.data.id, decision: decision }, "*");
        }
      );
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_REVIEW_DECISION_SET", ok: false, error: "context_invalidated" }, "*");
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

  // Dev/feature flag setter (allowlisted). Both flags are individually safe: `hd_debug`
  // only unlocks the review-mode-gated ATS test bridge (can't submit), `atsWiring` only
  // lets a campaign navigate external jobs to Greenhouse/Lever (product behavior, still
  // fail-closed in phase_ats). Used to enable P4 wiring for an observed live run.
  if (typeof e.data === "object" && e.data.type === "HIREDROP_SET_FLAG" && typeof e.data.flag === "string") {
    const ALLOW = ["hd_debug", "atsWiring"];
    if (ALLOW.indexOf(e.data.flag) === -1) {
      window.postMessage({ type: "HIREDROP_FLAG_SET", flag: e.data.flag, ok: false, reason: "not allowlisted" }, "*");
    } else {
      try {
        chrome.storage.local.set({ [e.data.flag]: e.data.on === true }, function () {
          window.postMessage({ type: "HIREDROP_FLAG_SET", flag: e.data.flag, ok: true, value: e.data.on === true }, "*");
        });
      } catch (ex) {
        window.postMessage({ type: "HIREDROP_FLAG_SET", flag: e.data.flag, ok: false, error: "context_invalidated" }, "*");
      }
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

  // Dashboard logout → wipe the extension's user-scoped state (durable key,
  // profile, dedup history, platform statuses). Without this relay the key
  // outlived the dashboard session and the extension kept acting as the
  // logged-out user. Must round-trip through the service worker (storage
  // writes + campaign stop live there).
  if (typeof e.data === "object" && e.data.type === "HIREDROP_LOGOUT") {
    try {
      chrome.runtime.sendMessage({ type: "LOGOUT" }, function (resp) {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
        window.postMessage({ type: "HIREDROP_LOGGED_OUT", ok: !!(resp && resp.loggedOut), error: err }, "*");
      });
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_LOGGED_OUT", ok: false, error: "context_invalidated" }, "*");
    }
  }

  // Live campaign state for the dashboard Campaign Live view. Same direct-storage
  // pattern as HIREDROP_GET_PLATFORM_CONNECTIONS (bypasses a possibly-stale MV3
  // service worker). captchaWaiting = a human hand-off is pending (captcha /
  // security check) — the dashboard shows a "solve the captcha" CTA off it.
  if (typeof e.data === "object" && e.data.type === "HIREDROP_GET_LIVE_STATE") {
    try {
      chrome.storage.local.get(["campaignRunning", "captchaWaiting", "reviewPending", "reviewMode"], function (data) {
        const err = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
        window.postMessage(
          {
            type: "HIREDROP_LIVE_STATE",
            ok: !err,
            campaignRunning: !!(data && data.campaignRunning),
            captchaWaiting: (data && data.captchaWaiting) || null,
            // Tap-mode: a filled application awaiting the human's Approve/Skip on the
            // dashboard card, plus whether tap (review) mode is on at all.
            reviewPending: (data && data.reviewPending) || null,
            reviewMode: !!(data && data.reviewMode),
            error: err,
          },
          "*"
        );
      });
    } catch (ex) {
      window.postMessage({ type: "HIREDROP_LIVE_STATE", ok: false, campaignRunning: false, captchaWaiting: null, reviewPending: null, reviewMode: false, error: "context_invalidated" }, "*");
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
