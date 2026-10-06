// LinkedIn lane — GROUNDWORK ONLY (docs/handoff/linkedin.md). Nothing in this file makes the
// extension act on LinkedIn by default, and nothing in it ever starts a LinkedIn campaign.
//
// Why it looks like this:
//   * LinkedIn restricts accounts for automation fast (User Agreement §8.2), so the lane is a
//     dev/beta switch, `linkedinBeta` in chrome.storage.local, OFF unless someone turns it on.
//   * #167 took *.linkedin.com out of the manifest (host_permissions + content_scripts), and it
//     stays out: putting it back would show every Web Store user a new permission warning.
//     Access is asked for at runtime instead — linkedin.com is inside the manifest's
//     optional_host_permissions (<all_urls>), so chrome.permissions.request() can grant just
//     that origin on a click in an extension page (the popup's dev section), exactly the way
//     pill-everywhere.js asks for <all_urls>.
//   * Only flag ON + origin GRANTED registers content.js on LinkedIn. Either one going away
//     unregisters it.
//   * Registered or not, a campaign never STARTS on LinkedIn here (hdCampaignStartPlatforms),
//     and content.js keeps its LinkedIn phases inert (LINKEDIN_APPLY_ENABLED there). With the
//     script registered, the only LinkedIn thing that answers is the capture kit — a manual
//     "save this page, PII masked" for building real fixtures.
//
// Loaded into the service worker with importScripts(); the pure part is exported for
// tests/linkedin-beta.test.js.

const HD_LINKEDIN_BETA_FLAG = "linkedinBeta";
const HD_LINKEDIN_SCRIPT_ID = "hd-linkedin-beta";
const HD_LINKEDIN_ORIGINS = ["https://www.linkedin.com/*"];

// Flipped to true only by the PR that ships a live-verified LinkedIn apply path. Until then a
// campaign never opens on LinkedIn, whatever the user's saved platform list says (QuickActions
// in the dashboard notes old prefs can still carry "linkedin").
const HD_LINKEDIN_CAMPAIGN_ENABLED = false;

// Pure: the two conditions, both required. Strict booleans — a missing flag is OFF.
function hdLinkedInShouldRegister(flag, granted) {
  return flag === true && granted === true;
}

// Pure: manifest -> the dynamic registration. Takes the static entry that carries content.js
// so runAt and the file list follow the manifest, MINUS pill.js: with the optional <all_urls>
// access granted, pill-everywhere.js already injects pill.js on linkedin.com (it is not a
// manifest host, so it is not excluded there), and a second copy in the same isolated world
// redeclares pill.js's top-level consts — a SyntaxError. content.js does not need pill.js.
function hdLinkedInBetaScript(manifest) {
  const entry = (manifest.content_scripts || []).find((cs) => (cs.js || []).includes("content.js")) || {};
  const js = (entry.js || ["content.js"]).filter((f) => f !== "pill.js");
  return {
    id: HD_LINKEDIN_SCRIPT_ID,
    js,
    matches: HD_LINKEDIN_ORIGINS.slice(),
    runAt: entry.run_at || "document_idle",
    allFrames: false,
    persistAcrossSessions: true,
  };
}

// Pure: the board platforms a campaign may OPEN on. LinkedIn stays in AUTO_APPLY_PLATFORMS
// (login links, labels) but is filtered out of every campaign-start decision.
function hdCampaignStartPlatforms(autoApplyPlatforms) {
  return (autoApplyPlatforms || []).filter((p) => HD_LINKEDIN_CAMPAIGN_ENABLED || p !== "linkedin");
}

// Pure: a selection whose ONLY runnable choice is LinkedIn. Without this guard such a run
// would fall through to pickPrimaryPlatform's "indeed" default — a board the user never
// picked. Refuse instead, with a reason.
function hdLinkedInOnlySelection(platforms, startPlatforms, atsPlatforms) {
  const list = platforms || [];
  if (!list.includes("linkedin")) return false;
  if (HD_LINKEDIN_CAMPAIGN_ENABLED) return false;
  const runnable = (startPlatforms || []).concat(atsPlatforms || []);
  return !list.some((p) => runnable.includes(p));
}

// Bring the registration in line with flag + permission. Idempotent; safe from every lifecycle
// event. `api` is chrome in the worker and a fake in the tests.
async function hdLinkedInBetaSync(api) {
  const c = api || (typeof chrome !== "undefined" ? chrome : null);
  if (!c || !c.scripting || !c.permissions) return null;
  try {
    const store = await c.storage.local.get(HD_LINKEDIN_BETA_FLAG);
    const flag = store ? store[HD_LINKEDIN_BETA_FLAG] : undefined;
    const granted = await c.permissions.contains({ origins: HD_LINKEDIN_ORIGINS });
    const existing = await c.scripting.getRegisteredContentScripts({ ids: [HD_LINKEDIN_SCRIPT_ID] });
    if (!hdLinkedInShouldRegister(flag, granted)) {
      if (existing.length) await c.scripting.unregisterContentScripts({ ids: [HD_LINKEDIN_SCRIPT_ID] });
      return false;
    }
    const script = hdLinkedInBetaScript(c.runtime.getManifest());
    if (existing.length) await c.scripting.updateContentScripts([script]);
    else await c.scripting.registerContentScripts([script]);
    return true;
  } catch (e) {
    console.warn("[HireDrop] linkedin-beta sync failed:", e && e.message);
    return null;
  }
}

if (typeof module === "object" && module.exports) {
  module.exports = {
    HD_LINKEDIN_BETA_FLAG,
    HD_LINKEDIN_SCRIPT_ID,
    HD_LINKEDIN_ORIGINS,
    HD_LINKEDIN_CAMPAIGN_ENABLED,
    hdLinkedInShouldRegister,
    hdLinkedInBetaScript,
    hdCampaignStartPlatforms,
    hdLinkedInOnlySelection,
    hdLinkedInBetaSync,
  };
}
