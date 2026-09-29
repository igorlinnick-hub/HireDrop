// Indeed session renewal on the way in. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/indeed-session-renew.test.js
//
// The bug (live 2026-09-28, ext 1.8.21): Indeed's working session had expired while its
// year-long key was still in the browser. The walk opened www.indeed.com — where search
// works signed out — so the expiry surfaced only at the first Apply, which bounced to
// secure.indeed.com/auth; content.js read that as a login wall and the run waited 5 min
// for a human. One navigation to secure.indeed.com/auth?continue=<home> in the same
// browser came back SIGNED IN with no click. So every entry to Indeed now goes that way,
// and the auth page with a return address gets time to bounce before anyone calls it a wall.
//
// BEHAVIORAL: the real platformEntryUrl / settleIndeedAuthTransit, cut from the source and run.
// STRUCTURAL: every place the walk enters a board uses the entry URL, and every reader of
//             Indeed's login state waits out the transit first.

const fs = require("fs");
const path = require("path");
const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const CS = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

function cut(src, header) {
  const i = src.indexOf(header);
  if (i < 0) return null;
  const j = src.indexOf("\n}\n", i);
  const k = src.indexOf("\n  }\n", i);
  const end = header.startsWith("  ") ? k + 4 : j + 3;
  return src.slice(i, end);
}

(async () => {
  // ---- BEHAVIORAL: platformEntryUrl ---------------------------------------------------
  const homeSrc = cut(BG, "function platformHomeUrl(platform) {");
  const entrySrc = cut(BG, "function platformEntryUrl(platform) {");
  check("platformEntryUrl exists", !!entrySrc, "no entry-URL function in background.js");
  if (entrySrc && homeSrc) {
    const platformEntryUrl = new Function(`${homeSrc}\n${entrySrc}\nreturn platformEntryUrl;`)();
    const u = new URL(platformEntryUrl("indeed"));
    check("Indeed enters through its sign-in page", u.hostname === "secure.indeed.com" && u.pathname === "/auth",
      u.href);
    check("…with the homepage as the return address",
      u.searchParams.get("continue") === "https://www.indeed.com/", u.searchParams.get("continue"));
    check("ZipRecruiter still enters on its homepage",
      platformEntryUrl("ziprecruiter") === "https://www.ziprecruiter.com/");
    check("LinkedIn still enters on its homepage",
      platformEntryUrl("linkedin") === "https://www.linkedin.com/");
  }

  // ---- STRUCTURAL: every board entry goes through platformEntryUrl ----------------------
  // platformHomeUrl may only be CALLED from inside platformEntryUrl now; a new call site
  // elsewhere reopens Indeed without renewal.
  const calls = [...BG.matchAll(/platformHomeUrl\(/g)].map((m) => m.index);
  const defIdx = BG.indexOf("function platformHomeUrl(");
  const entryIdx = BG.indexOf("function platformEntryUrl(");
  const entryEnd = BG.indexOf("\n}\n", entryIdx);
  const stray = calls.filter((i) => i !== defIdx + "function ".length && !(i > entryIdx && i < entryEnd));
  check("no board entry bypasses platformEntryUrl", stray.length === 0,
    `${stray.length} direct platformHomeUrl() call(s) outside platformEntryUrl`);
  check("all four entry points use it (start, pool head, pool warm, board switch)",
    (BG.match(/platformEntryUrl\(/g) || []).length >= 5, "expected definition + 4 call sites");

  // ---- BEHAVIORAL: settleIndeedAuthTransit --------------------------------------------
  const settleSrc = cut(CS, "  async function settleIndeedAuthTransit() {");
  check("settleIndeedAuthTransit exists", !!settleSrc, "no transit grace in content.js");
  if (settleSrc) {
    const run = async (href) => {
      const slept = [];
      const loc = new URL(href);
      const fn = new Function("window", "sleep", "INDEED_RENEW_GRACE_MS",
        `${settleSrc}\nreturn settleIndeedAuthTransit;`)(
        { location: { hostname: loc.hostname, search: loc.search } },
        async (ms) => { slept.push(ms); }, 10000);
      await fn();
      return slept;
    };
    const t1 = await run("https://secure.indeed.com/auth?hl=en_US&co=US&continue=https%3A%2F%2Fwww.indeed.com%2F");
    check("the renewal entry waits for Indeed to bounce", t1.length === 1 && t1[0] >= 5000, JSON.stringify(t1));
    // The Apply bounce of 09-28 also carries a return address — same chance to renew.
    const t2 = await run("https://secure.indeed.com/auth?continue=https%3A%2F%2Fsmartapply.indeed.com%2Fx");
    check("an Apply bounce with a return address waits too", t2.length === 1, JSON.stringify(t2));
    const t3 = await run("https://secure.indeed.com/account/login");
    check("a bare sign-in page does not wait (it IS the wall)", t3.length === 0, JSON.stringify(t3));
    const t4 = await run("https://www.indeed.com/?continue=x");
    check("a normal Indeed page never waits", t4.length === 0, JSON.stringify(t4));
  }

  // ---- STRUCTURAL: readers of Indeed login state wait out the transit first ------------
  const rpa = cut(CS, "  async function reportPlatformAuth() {") || "";
  check("reportPlatformAuth waits before judging Indeed",
    rpa.indexOf("settleIndeedAuthTransit") > -1 &&
    rpa.indexOf("settleIndeedAuthTransit") < rpa.indexOf("detectPlatformAuth("),
    "a transit page would be written down as logged_out");
  const wallIdx = CS.indexOf("const authStatus = isAtsGuest");
  const before = CS.slice(Math.max(0, wallIdx - 400), wallIdx);
  check("the login-wall pause waits before judging Indeed", /settleIndeedAuthTransit\(\)/.test(before),
    "the 5-min pause would open on a page that was about to bounce");
  const warmIdx = CS.indexOf("await sessionWarmup();");
  const beforeWarm = CS.slice(Math.max(0, warmIdx - 400), warmIdx);
  check("warmup cannot navigate away mid-renewal", /settleIndeedAuthTransit\(\)/.test(beforeWarm),
    "sessionWarmup could leave the auth page before the redirect");

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
