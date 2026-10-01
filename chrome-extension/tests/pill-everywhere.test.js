// Fixture test for the EDGE PILL ON EVERY SITE (pill-everywhere.js), against the real
// manifest.json.
//
//   node <repo>/jobflow/chrome-extension/tests/pill-everywhere.test.js
//
// What is pinned:
//   1. The broad access stays OPTIONAL. A required <all_urls> would show every current user
//      "read and change all your data on all websites" on update and disable the extension
//      until they accept — the very thing the opt-in exists to avoid.
//   2. The dynamic registration excludes every host a static content script already covers.
//      On the job boards pill.js is already injected — a second copy redeclares its
//      top-level consts in the same isolated world (SyntaxError) — and hiredrop.io has its
//      own UI.
//   3. Open tabs get the pill only where the registration would have put it.

const fs = require("fs");
const path = require("path");
const { hdPillEverywhereScript, hdPillEverywhereCovers, HD_PILL_EVERYWHERE_ORIGINS } = require("../pill-everywhere.js");

const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));

let failures = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${name}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`);
  } else {
    console.log(`ok   ${name}`);
  }
}

// --- 1. the access is optional ---------------------------------------------------------

check("<all_urls> is an optional host permission", (manifest.optional_host_permissions || []).includes("<all_urls>"), true);
check("<all_urls> is NOT a required host permission", (manifest.host_permissions || []).includes("<all_urls>"), false);
check(
  "no static content script reaches every site",
  (manifest.content_scripts || []).some((cs) => (cs.matches || []).some((m) => m === "<all_urls>" || /^(\*|https?):\/\/\*\/\*$/.test(m))),
  false,
);
check("the popup asks for the same origins the worker checks", HD_PILL_EVERYWHERE_ORIGINS, ["<all_urls>"]);
check("scripting permission is present (registerContentScripts)", manifest.permissions.includes("scripting"), true);

// --- 2. the registration ----------------------------------------------------------------

const script = hdPillEverywhereScript(manifest);
check("registers pill.js only", script.js, ["pill.js"]);
check("top frame only", script.allFrames, false);
const staticMatches = [...new Set(manifest.content_scripts.flatMap((cs) => cs.matches))];
check("excludes every statically covered host", staticMatches.every((m) => script.excludeMatches.includes(m)), true);
check("hiredrop.io is excluded", script.excludeMatches.includes("https://hiredrop.io/*"), true);
check("a job board is excluded", script.excludeMatches.includes("https://*.indeed.com/*"), true);

// A platform added to the manifest later is excluded with no edit here.
const grown = JSON.parse(JSON.stringify(manifest));
grown.content_scripts[0].matches.push("https://*.myworkdayjobs.com/*");
check("a new platform in the manifest is excluded automatically", hdPillEverywhereScript(grown).excludeMatches.includes("https://*.myworkdayjobs.com/*"), true);

// --- 3. open tabs -----------------------------------------------------------------------

const covers = (u) => hdPillEverywhereCovers(u, manifest);
check("an ordinary site is covered", covers("https://mail.google.com/mail/u/0/#inbox"), true);
check("plain http is covered", covers("http://example.com/"), true);
check("Indeed is not (static script already there)", covers("https://www.indeed.com/viewjob?jk=1"), false);
check("bare apex of a wildcard board is not", covers("https://indeed.com/"), false);
check("Greenhouse subdomain is not", covers("https://job-boards.greenhouse.io/acme/jobs/1"), false);
check("hiredrop.io is not", covers("https://hiredrop.io/dashboard"), false);
check("a lookalike host is covered (not a suffix match by accident)", covers("https://notindeed.com/"), true);
check("chrome:// is not", covers("chrome://extensions/"), false);
check("garbage is not", covers("not a url"), false);

// --- 4. the dashboard's "Tracking pop-up" switch ---------------------------------------

const read = (f) => fs.readFileSync(path.join(__dirname, "..", f), "utf8");
check("the Allow window exists", fs.existsSync(path.join(__dirname, "..", "pill-allow.html")), true);
check("the Allow window asks for the same origins", read("pill-allow.js").includes('origins: ["<all_urls>"]'), true);
check("ping.js bridges GET and SET", ["HIREDROP_GET_PILL_EVERYWHERE", "HIREDROP_SET_PILL_EVERYWHERE", "HIREDROP_PILL_EVERYWHERE"].every((t) => read("ping.js").includes(t)), true);
check("background answers both", ['case "PILL_EVERYWHERE_STATE"', 'case "PILL_EVERYWHERE_SET"'].every((t) => read("background.js").includes(t)), true);
check("no web_accessible_resources (pages can't probe the extension)", "web_accessible_resources" in manifest, false);

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall pill-everywhere checks passed");
