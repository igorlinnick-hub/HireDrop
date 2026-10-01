// Edge pill on every site — opt-in, through an OPTIONAL host permission.
//
// The manifest injects pill.js on the job boards only. Users rarely open those boards (the
// run applies by itself), so a pill that lives there alone is seldom seen. Asking for
// <all_urls> up front is the wrong fix: Chrome would show "read and change all your data on
// all websites" and DISABLE the extension for every current user until they accept.
//
// So the broad access is optional: the popup asks for it on a click, and only then is
// pill.js registered dynamically for every http(s) page. Whoever never clicks keeps exactly
// what they have now. Revoking the access (popup or chrome://extensions) drops the script.
//
// Loaded into the service worker with importScripts(); the pure part is exported for
// tests/pill-everywhere.test.js.

const HD_PILL_EVERYWHERE_ID = "hd-pill-everywhere";
const HD_PILL_EVERYWHERE_ORIGINS = ["<all_urls>"];

// Pure: manifest -> the dynamic registration. Every host that already gets a static content
// script is excluded: on the job boards pill.js is already there (a second injection
// redeclares its top-level consts in the same isolated world = SyntaxError), and
// hiredrop.io has its own UI. Derived from the manifest, so a new platform added there is
// excluded here without anyone remembering to.
function hdPillEverywhereScript(manifest) {
  const exclude = [];
  for (const cs of manifest.content_scripts || []) {
    for (const m of cs.matches || []) if (!exclude.includes(m)) exclude.push(m);
  }
  return {
    id: HD_PILL_EVERYWHERE_ID,
    js: ["pill.js"],
    matches: ["https://*/*", "http://*/*"],
    excludeMatches: exclude,
    runAt: "document_idle",
    allFrames: false,
    persistAcrossSessions: true,
  };
}

// Pure: is this tab's URL one the dynamic script covers? (For injecting into tabs that
// were already open when access was granted — a registration only reaches new loads.)
function hdPillEverywhereCovers(url, manifest) {
  if (!/^https?:\/\//i.test(url || "")) return false;
  let u;
  try { u = new URL(url); } catch { return false; }
  const host = u.hostname;
  for (const cs of manifest.content_scripts || []) {
    for (const m of cs.matches || []) {
      const mm = /^(\*|https?):\/\/([^/]+)\//.exec(m);
      if (!mm) continue;
      const pat = mm[2];
      if (pat.startsWith("*.")) {
        const base = pat.slice(2);
        if (host === base || host.endsWith("." + base)) return false;
      } else if (host === pat) {
        return false;
      }
    }
  }
  return true;
}

// Bring the registration in line with the permission: granted -> registered (and up to date
// with this version's manifest), not granted -> gone. Idempotent; safe to call from every
// lifecycle event.
async function hdPillEverywhereSync({ injectOpenTabs = false } = {}) {
  try {
    const granted = await chrome.permissions.contains({ origins: HD_PILL_EVERYWHERE_ORIGINS });
    const existing = await chrome.scripting.getRegisteredContentScripts({ ids: [HD_PILL_EVERYWHERE_ID] });
    if (!granted) {
      if (existing.length) await chrome.scripting.unregisterContentScripts({ ids: [HD_PILL_EVERYWHERE_ID] });
      return false;
    }
    const manifest = chrome.runtime.getManifest();
    const script = hdPillEverywhereScript(manifest);
    if (existing.length) await chrome.scripting.updateContentScripts([script]);
    else await chrome.scripting.registerContentScripts([script]);

    if (injectOpenTabs) {
      const tabs = await chrome.tabs.query({});
      for (const t of tabs) {
        if (!t.id || !hdPillEverywhereCovers(t.url, manifest)) continue;
        // Best effort: discarded tabs, the Web Store, PDF viewers etc. refuse — that's fine.
        chrome.scripting.executeScript({ target: { tabId: t.id }, files: ["pill.js"] }).catch(() => {});
      }
    }
    return true;
  } catch (e) {
    console.warn("[HireDrop] pill-everywhere sync failed:", e && e.message);
    return null;
  }
}

if (typeof module === "object" && module.exports) {
  module.exports = { hdPillEverywhereScript, hdPillEverywhereCovers, HD_PILL_EVERYWHERE_ORIGINS };
}
