// Indeed login shows up on the dashboard the moment it happens. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/indeed-auth-instant.test.js
//
// The bug (live 2026-10-08): the dashboard's Connect opens secure.indeed.com/auth; a finished
// sign-in lands on secure.indeed.com/settings/account, whose header carries AccountMenu +
// SignOut (probed live in Igor's Chrome: data-gnav-element-name = …, AccountMenu, …,
// SignOut). detectPlatformAuth read the HOST first — "secure.indeed.com is the login wall" —
// and wrote logged_out, so the card kept "Signed out — log back in" over a minute after the
// person had logged in, until www.indeed.com happened to be opened. On top of that the card
// only learned of a change on its next 8 s poll.
//
// BEHAVIORAL: the real detectPlatformAuth on each page shape; the real ping.js pushing a
//             storage change to the page.
// STRUCTURAL: the in-page watcher re-reads on a header redraw, not only every 45 s.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM, VirtualConsole } = require("jsdom");

const CS = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const PING = fs.readFileSync(path.join(__dirname, "..", "ping.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}
function slice(src, from, to) {
  const a = src.indexOf(from);
  const b = src.indexOf(to, a);
  if (a < 0 || b < 0) {
    console.error(`markers moved: ${from} … ${to}`);
    process.exit(2);
  }
  return src.slice(a, b);
}

const DETECT = slice(CS, "  const INDEED_APPLY_HOSTS =", "  // Persist + report the current platform's login state.");

// The header markers as the live pages carry them (attribute values from the 10-08 probe).
const SIGNED_IN_HEADER =
  '<header><nav><a data-gnav-element-name="Myjobs" href="https://myjobs.indeed.com"></a>' +
  '<button data-gnav-element-name="AccountMenu"></button>' +
  '<a data-gnav-element-name="Resume" href="https://profile.indeed.com/"></a>' +
  '<a data-gnav-element-name="SignOut" href="/account/logout">Sign out</a></nav></header>';
const SIGNED_OUT_HEADER = '<header><nav><a data-gnav-element-name="SignIn" href="https://secure.indeed.com/auth">Sign in</a></nav></header>';

function detect(url, body) {
  const { window } = new JSDOM(`<body>${body}</body>`, { url, virtualConsole: new VirtualConsole() });
  const box = { document: window.document, window: { location: window.location }, detectPlatform: () => "indeed" };
  vm.createContext(box);
  vm.runInContext(`${DETECT}\nthis.detectPlatformAuth = detectPlatformAuth;`, box);
  return box.detectPlatformAuth("indeed");
}

// ---- BEHAVIORAL: detectPlatformAuth -----------------------------------------------------
check("signed in, landed on secure.indeed.com/settings/account → connected",
  detect("https://secure.indeed.com/settings/account", SIGNED_IN_HEADER + "<main>Account settings</main>") === "connected");
check("signed in, secure.indeed.com/settings → connected",
  detect("https://secure.indeed.com/settings?hl=en_US", SIGNED_IN_HEADER) === "connected");
check("sign-in page (secure.indeed.com/auth, no account menu) → logged_out",
  detect("https://secure.indeed.com/auth?hl=en_US", "<form><input type=email></form>") === "logged_out");
check("old login page (secure.indeed.com/account/login) → logged_out",
  detect("https://secure.indeed.com/account/login", "<form></form>") === "logged_out");
check("smartapply bounced to a SignIn header → logged_out",
  detect("https://smartapply.indeed.com/beta/indeedapply/form", SIGNED_OUT_HEADER) === "logged_out");
check("www header says SignIn → unknown (search host sessions differ, #171)",
  detect("https://www.indeed.com/", SIGNED_OUT_HEADER) === "unknown");
check("www header with the account menu → connected",
  detect("https://www.indeed.com/", SIGNED_IN_HEADER) === "connected");
check("secure.indeed.com page with neither marker → unknown, not logged_out",
  detect("https://secure.indeed.com/settings/account", "<main>loading…</main>") === "unknown");

// ---- BEHAVIORAL: ping.js pushes a login change to the dashboard -------------------------
{
  const posted = [];
  let onChanged = null;
  const box = {
    window: { addEventListener: () => {}, postMessage: (m) => posted.push(m) },
    chrome: {
      storage: { onChanged: { addListener: (fn) => { onChanged = fn; } }, local: { get: () => {} } },
      runtime: { sendMessage: () => {}, lastError: null },
    },
    document: { addEventListener: () => {} },
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
  };
  vm.createContext(box);
  vm.runInContext(PING, box);
  check("ping.js listens for storage changes", typeof onChanged === "function");
  if (onChanged) {
    const conns = { indeed: { status: "connected", checkedAt: "2026-10-08T03:00:00Z", host: "secure.indeed.com" } };
    onChanged({ platformConnections: { oldValue: { indeed: { status: "logged_out" } }, newValue: conns } }, "local");
    const m = posted.find((x) => x && x.type === "HIREDROP_PLATFORM_CONNECTIONS");
    check("a new login state reaches the page in the message the dashboard already reads",
      m && m.ok === true && m.connections.indeed.status === "connected", JSON.stringify(posted));
    posted.length = 0;
    onChanged({ campaignRunning: { newValue: true } }, "local");
    onChanged({ platformConnections: { newValue: conns } }, "sync");
    check("other keys / other areas push nothing", posted.length === 0, JSON.stringify(posted));
    onChanged({ platformConnections: { oldValue: conns } }, "local");
    check("statuses wiped (dashboard logout) push an empty map, not undefined",
      posted[0] && JSON.stringify(posted[0].connections) === "{}", JSON.stringify(posted));
  }
}

// ---- STRUCTURAL: the in-page watcher reacts to a header redraw -------------------------
{
  const watch = slice(CS, "  function watchPlatformAuth() {", "  // The new description block ships");
  check("watchPlatformAuth re-reads on DOM changes (MutationObserver), throttled",
    /new MutationObserver\(/.test(watch) && /setTimeout\(\(\) => \{ pending = null; recheck\(\); \}, 500\)/.test(watch));
  check("and still writes only when the answer changed",
    /if \(!prev \|\| prev\.status !== status\) await reportPlatformAuth\(\);/.test(watch));
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
