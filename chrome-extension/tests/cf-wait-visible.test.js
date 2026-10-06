// The Cloudflare "Just a moment" wait must be visible in the activity log. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/cf-wait-visible.test.js
//
// 10-06, Indeed /viewjob: three loads 64 s apart (60 s wait + reload, twice) and not one
// line in the activity log between them — the wait wrote only to the popup log, so three
// minutes of a Cloudflare interstitial read in prod as a frozen run. This loads the WHOLE
// content.js on a "Just a moment" page with a fake background and checks the durable
// (LOG_BACKEND) lines.

const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

const URL_ = "https://www.indeed.com/viewjob?jk=85ded862997ca354";
const html = "<html><head><title>Just a moment...</title></head><body><div>Checking your browser</div></body></html>";
const dom = new JSDOM(html, { url: URL_, runScripts: "outside-only", pretendToBeVisual: true, virtualConsole: new VirtualConsole() });
const w = dom.window;
const store = { campaignRunning: true, campaignTabId: 7, platforms: ["indeed"], campaignCaps: { dailyTotal: 30, perPlatform: 15 } };
const backend = [];
w.chrome = {
  runtime: {
    id: "x", lastError: null,
    getManifest: () => ({ version: "test" }), getURL: (p) => p,
    onMessage: { addListener: () => {} },
    sendMessage: (msg, cb) => {
      if (msg.type === "SLEEP") { setTimeout(() => cb && cb({ ok: true }), 5); return; }
      if (msg.type === "LOG_BACKEND") backend.push(msg.text);
      const r = msg.type === "AM_I_CAMPAIGN_TAB" ? { known: true, isCampaignTab: true } : { ok: true };
      setTimeout(() => cb && cb(r), 1);
    },
  },
  storage: {
    local: {
      get: async (k) => {
        const keys = k == null ? Object.keys(store) : [].concat(typeof k === "object" && !Array.isArray(k) ? Object.keys(k) : k);
        const o = {}; for (const x of keys) if (x in store) o[x] = store[x]; return o;
      },
      set: async (o) => { Object.assign(store, o); },
      remove: async (k) => { for (const x of [].concat(k)) delete store[x]; },
    },
    session: { get: async () => ({}), set: async () => {} },
    onChanged: { addListener: () => {} },
  },
};
w.document.elementFromPoint = () => null;
w.scrollTo = () => {};
w.HTMLElement.prototype.scrollIntoView = function () {};
w.fetch = async () => ({ ok: true, json: async () => ({}), text: async () => "" });
w.eval(fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8"));

setTimeout(() => {
  const has = (re) => backend.some((t) => re.test(t));
  check("the wait is in the activity log (where + how long)", has(/Cloudflare check on www\.indeed\.com\/viewjob — waiting up to 60 s/), JSON.stringify(backend));
  check("the reload is in the activity log", has(/Cloudflare didn't clear in 60 s .* reloading the tab \(try 1\/2\)/), JSON.stringify(backend));
  if (failures) { console.log(`\n${failures} failed`); process.exit(1); }
  console.log("\nall passed");
  process.exit(0);
}, 6000);
