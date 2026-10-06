// LinkedIn lane groundwork (linkedin-beta.js, docs/handoff/linkedin.md): the extension must
// not act on LinkedIn unless a human turned it on, and a campaign must never OPEN there.
//
//   node <repo>/jobflow/chrome-extension/tests/linkedin-beta.test.js
//
// What is pinned:
//   1. Registration follows flag AND permission: off → none; on without the linkedin.com
//      grant → none; on + grant → content.js registered for www.linkedin.com (no pill.js);
//      either one going away → unregistered.
//   2. The manifest is untouched: no LinkedIn host permission or static match (#167 stays),
//      and the origin the popup asks for sits inside optional_host_permissions.
//   3. A campaign never starts on LinkedIn: the primary board, the pool opener and the
//      "linkedin is the only choice" refusal, run from background.js's own source.
//   4. content.js keeps its LinkedIn phases inert even when injected.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const beta = require("../linkedin-beta.js");

const EXT = path.join(__dirname, "..");
const manifest = JSON.parse(fs.readFileSync(path.join(EXT, "manifest.json"), "utf8"));
const BG = fs.readFileSync(path.join(EXT, "background.js"), "utf8");
const CONTENT = fs.readFileSync(path.join(EXT, "content.js"), "utf8");

let failures = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${name}${ok ? "" : `  (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`}`);
}

// A fake chrome with just the calls hdLinkedInBetaSync makes.
function fakeChrome({ flag, granted }) {
  const registered = new Map();
  const calls = [];
  const c = {
    storage: { local: { get: async () => (flag === undefined ? {} : { linkedinBeta: flag }) } },
    permissions: {
      contains: async (q) => {
        calls.push(["contains", q]);
        return granted;
      },
    },
    runtime: { getManifest: () => manifest },
    scripting: {
      getRegisteredContentScripts: async ({ ids }) => ids.filter((id) => registered.has(id)).map((id) => registered.get(id)),
      registerContentScripts: async (list) => { for (const s of list) registered.set(s.id, s); calls.push(["register"]); },
      updateContentScripts: async (list) => { for (const s of list) registered.set(s.id, s); calls.push(["update"]); },
      unregisterContentScripts: async ({ ids }) => { for (const id of ids) registered.delete(id); calls.push(["unregister"]); },
    },
  };
  return { c, registered, calls, set(next) { if ("flag" in next) flag = next.flag; if ("granted" in next) granted = next.granted; } };
}

(async () => {
  // --- 1. registration ---------------------------------------------------------------------
  {
    const f = fakeChrome({ flag: undefined, granted: true });
    check("flag never set (default) → not registered", await beta.hdLinkedInBetaSync(f.c), false);
    check("flag never set → registry empty", f.registered.size, 0);
  }
  {
    const f = fakeChrome({ flag: false, granted: true });
    await beta.hdLinkedInBetaSync(f.c);
    check("flag off + permission → not registered", f.registered.size, 0);
  }
  {
    const f = fakeChrome({ flag: true, granted: false });
    check("flag on without permission → not registered", await beta.hdLinkedInBetaSync(f.c), false);
    check("flag on without permission → registry empty", f.registered.size, 0);
    check("it asked about linkedin.com only",
      f.calls.find((x) => x[0] === "contains")[1], { origins: ["https://www.linkedin.com/*"] });
  }
  {
    const f = fakeChrome({ flag: "true", granted: true });
    await beta.hdLinkedInBetaSync(f.c);
    check("a truthy non-boolean flag is still OFF", f.registered.size, 0);
  }
  {
    const f = fakeChrome({ flag: true, granted: true });
    check("flag on + permission → registered", await beta.hdLinkedInBetaSync(f.c), true);
    const s = f.registered.get(beta.HD_LINKEDIN_SCRIPT_ID);
    check("registered for www.linkedin.com", s && s.matches, ["https://www.linkedin.com/*"]);
    check("registers content.js, not pill.js", s && s.js, ["content.js"]);
    check("top frame only", s && s.allFrames, false);
    check("same run_at as the manifest entry", s && s.runAt, manifest.content_scripts[0].run_at);

    await beta.hdLinkedInBetaSync(f.c);
    check("second sync updates, does not double-register", f.calls.filter((x) => x[0] === "register").length, 1);

    f.set({ flag: false });
    await beta.hdLinkedInBetaSync(f.c);
    check("flag goes off → unregistered", f.registered.size, 0);

    f.set({ flag: true });
    await beta.hdLinkedInBetaSync(f.c);
    f.set({ granted: false });
    await beta.hdLinkedInBetaSync(f.c);
    check("permission removed → unregistered", f.registered.size, 0);
  }

  // --- 2. the manifest is untouched ------------------------------------------------------
  const staticMatches = manifest.content_scripts.flatMap((cs) => cs.matches || []);
  check("no static content script on linkedin.com", staticMatches.some((m) => /linkedin/i.test(m)), false);
  check("no required linkedin host permission", (manifest.host_permissions || []).some((m) => /linkedin/i.test(m)), false);
  check("linkedin.com is grantable at runtime (<all_urls> optional)", (manifest.optional_host_permissions || []).includes("<all_urls>"), true);
  check("background loads linkedin-beta.js", /importScripts\([^)]*"linkedin-beta\.js"/.test(BG), true);

  // --- 3. a campaign never starts on LinkedIn --------------------------------------------
  check("campaign gate is closed in this build", beta.HD_LINKEDIN_CAMPAIGN_ENABLED, false);
  const autoMatch = /const AUTO_APPLY_PLATFORMS = (\[[^\]]*\]);/.exec(BG);
  const AUTO = autoMatch ? JSON.parse(autoMatch[1]) : null;
  check("AUTO_APPLY_PLATFORMS still read from background.js", Array.isArray(AUTO), true);
  const START = beta.hdCampaignStartPlatforms(AUTO);
  check("linkedin is not a campaign-start platform", START.includes("linkedin"), false);
  check("indeed and ziprecruiter still are", ["indeed", "ziprecruiter"].every((p) => START.includes(p)), true);

  function slice(from, to) {
    const a = BG.indexOf(from);
    const b = BG.indexOf(to, a);
    if (a < 0 || b < 0) { console.error(`markers moved in background.js: ${from} … ${to}`); process.exit(2); }
    return BG.slice(a, b);
  }
  const sandbox = { hdCampaignStartPlatforms: beta.hdCampaignStartPlatforms };
  vm.createContext(sandbox);
  vm.runInContext(
    slice("const AUTO_APPLY_PLATFORMS", "// ATS platforms applied to POOL-DRIVEN") +
    slice("const ATS_PLATFORMS", "// ---- STAGE ORDER") +
    slice("// ---- STAGE ORDER", "// ------------------------------------------------------------------------------------") +
    "\nthis.pickPrimaryPlatform = pickPrimaryPlatform; this.pickAtsOpener = pickAtsOpener;" +
    " this.CAMPAIGN_START_PLATFORMS = CAMPAIGN_START_PLATFORMS; this.ATS_PLATFORMS = ATS_PLATFORMS;",
    sandbox,
  );
  const { pickPrimaryPlatform, pickAtsOpener } = sandbox;
  check("primary: linkedin listed first → Indeed opens, not LinkedIn", pickPrimaryPlatform(["linkedin", "indeed"]), "indeed");
  check("primary: linkedin + ZR → ZR", pickPrimaryPlatform(["linkedin", "ziprecruiter"]), "ziprecruiter");
  check("primary: never linkedin, whatever the list", pickPrimaryPlatform(["linkedin"]) !== "linkedin", true);
  check("opener: linkedin is not a board that outranks the pool", pickAtsOpener(["linkedin", "greenhouse"]), "greenhouse");
  check("opener: a real board still outranks the pool", pickAtsOpener(["indeed", "greenhouse"]), null);

  const only = (list) => beta.hdLinkedInOnlySelection(list, sandbox.CAMPAIGN_START_PLATFORMS, sandbox.ATS_PLATFORMS);
  check("refuse: linkedin alone", only(["linkedin"]), true);
  check("refuse: linkedin + a discovery-only source", only(["linkedin", "remoteok"]), true);
  check("no refusal: linkedin + indeed", only(["linkedin", "indeed"]), false);
  check("no refusal: linkedin + lever (lever has its own rule)", only(["linkedin", "lever"]), false);
  check("no refusal: no linkedin at all", only(["remoteok"]), false);

  // START_CAMPAIGN wires the guard: "has a board" reads the start list, and the refusal exists.
  check("hasBoard reads CAMPAIGN_START_PLATFORMS",
    /const hasBoard = \(filters\.platforms \|\| \[\]\)\.some\(\(p\) => CAMPAIGN_START_PLATFORMS\.includes\(p\)\)/.test(BG), true);
  check("START_CAMPAIGN refuses a LinkedIn-only selection",
    /hdLinkedInOnlySelection\(filters\.platforms, CAMPAIGN_START_PLATFORMS, ATS_PLATFORMS\)[\s\S]{0,200}error: "linkedin_not_ready"/.test(BG), true);
  check("nothing else picks a start board off AUTO_APPLY_PLATFORMS",
    (BG.match(/AUTO_APPLY_PLATFORMS\.includes/g) || []).length, 1); // OPEN_PLATFORM_LOGIN only

  // --- 4. content.js stays inert on LinkedIn ---------------------------------------------
  check("content.js: LINKEDIN_APPLY_ENABLED is false", /const LINKEDIN_APPLY_ENABLED = false;/.test(CONTENT), true);
  const rp = CONTENT.indexOf("async function runPhase() {");
  const guard = CONTENT.indexOf('!LINKEDIN_APPLY_ENABLED && detectPlatform() === "linkedin"', rp);
  const firstAwait = CONTENT.indexOf("await isCampaignRunning()", rp);
  check("runPhase returns on LinkedIn before anything else runs", rp > 0 && guard > rp && guard < firstAwait, true);

  if (failures) {
    console.log(`\n${failures} failed`);
    process.exit(1);
  }
  console.log("\nall passed");
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
