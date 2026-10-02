// A job handed back after the Submit click must not stay "applied" in this browser. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/applied-rollback.test.js
//
// What went wrong (jobflow-2f, 10-02): content.js marks appliedUrls / appliedJobKeys BEFORE
// Submit (a navigating submit would lose an after-the-fact write). The post-click hand-back
// branches (Greenhouse email code, still-empty required fields, validation error) undid the
// count but not the marks, and background buildAtsQueue drops every marked posting — so 7 of
// 9 released GH hand-backs never came back to the queue, whatever the server said.
// Two fixes, both run here as the real functions: handBackJob forgets the marks (new cases),
// and buildAtsQueue first drops every OPEN hand-back from the sets (cases already stored).

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}
function extract(src, signature) {
  const at = src.indexOf(signature);
  if (at < 0) return null;
  const open = src.indexOf("{", at + signature.length - 1);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(at, i + 1); }
  }
  return null;
}

const GH = "https://job-boards.greenhouse.io/twilio/jobs/8170555";

(async () => {
  // ---- 1. handBackJob forgets the pre-click marks --------------------------------------
  {
    const parts = ["  function jobDedupKey(title, company) {", "  function localDay() {",
      "  async function addHandedBackKey(title, company) {", "  async function forgetAppliedJob(urls, title, company) {",
      "  async function handBackJob(reason, extra = {}) {"].map((sig) => extract(SRC, sig));
    check("handBackJob + forgetAppliedJob found", parts.every(Boolean));
    const store = {
      appliedUrls: ["https://x.io/a", `${GH}?gh_src=abc`, "https://x.io/b"],
      appliedJobKeys: ["a|x", "senior engineer|twilio", "b|y"],
    };
    const sent = [];
    const ctx = {
      window: { location: { href: GH } },
      storageGet: async (keys) => Object.fromEntries([].concat(keys).map((k) => [k, store[k]])),
      storageSet: async (o) => { Object.assign(store, o); return true; },
      sendMsg: async (m) => { sent.push(m); },
      collectUnfilledRequired: () => [], detectPlatform: () => "greenhouse",
    };
    vm.createContext(ctx);
    vm.runInContext(`${parts.join("\n")}\nglobalThis.hb = handBackJob;`, ctx);
    await ctx.hb("Greenhouse asked for the verification code", { title: "Senior Engineer", company: "Twilio", platform: "greenhouse" });
    check("the handed-back URL (with or without its query) is no longer 'applied'",
      !store.appliedUrls.some((u) => u.startsWith(GH)) && store.appliedUrls.length === 2, JSON.stringify(store.appliedUrls));
    check("…nor its title|company key, and the other jobs keep theirs",
      !store.appliedJobKeys.includes("senior engineer|twilio") && store.appliedJobKeys.length === 2, JSON.stringify(store.appliedJobKeys));
    check("the hand-back itself is still reported", sent.length === 1 && sent[0].type === "ATS_JOB_FAILED");
  }

  // ---- 2. buildAtsQueue repairs marks already stored ------------------------------------
  {
    const fn = extract(BG, "async function forgetHandedBackFromApplied() {");
    check("forgetHandedBackFromApplied found", !!fn);
    check("buildAtsQueue calls it before reading the applied sets",
      /if \(typeof forgetHandedBackFromApplied === "function"\) await forgetHandedBackFromApplied\(\);\s*\n\s*const dd = await chrome\.storage\.local\.get\(\["appliedUrls", "appliedJobKeys"\]\);/.test(extract(BG, "async function buildAtsQueue(platform, perPlatformCap) {") || ""));
    const run = async (handbacks, store) => {
      const ctx = {
        apiGet: async () => { if (handbacks instanceof Error) throw handbacks; return { handbacks }; },
        chrome: { storage: { local: {
          get: async (keys) => Object.fromEntries(keys.map((k) => [k, store[k]])),
          set: async (o) => { Object.assign(store, o); },
        } } },
      };
      vm.createContext(ctx);
      vm.runInContext(`${fn}\nglobalThis.f = forgetHandedBackFromApplied;`, ctx);
      await ctx.f();
      return store;
    };
    const s = await run([{ url: GH, job_title: "Senior  Engineer", company: "Twilio" }],
      { appliedUrls: [GH, "https://x.io/a"], appliedJobKeys: ["senior engineer|twilio", "a|x"] });
    check("an open hand-back stored as 'applied' is released", s.appliedUrls.join() === "https://x.io/a" && s.appliedJobKeys.join() === "a|x",
      JSON.stringify(s));
    const keep = { appliedUrls: [GH], appliedJobKeys: ["senior engineer|twilio"] };
    await run(new Error("503"), keep);
    check("hand-backs unreadable → the sets are left alone", keep.appliedUrls.length === 1 && keep.appliedJobKeys.length === 1);
  }

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
