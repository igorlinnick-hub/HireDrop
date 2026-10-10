// An ANSWERED hand-back must come back to the ATS queue even though it was marked applied
// before its Submit click; an unanswered one must not. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/applied-rollback.test.js
//
// What went wrong (jobflow-2f, 10-02): content.js marks appliedUrls / appliedJobKeys BEFORE
// Submit, a hand-back after the click left the marks, and background buildAtsQueue drops
// every marked posting — so those jobs never came back, even after the person answered.
// Why not release every open hand-back (skeptic on #320): one the person was told to finish
// by hand ("enter the code from your inbox and submit") may already be submitted; the marks
// are the only guard, and releasing it applies twice. The person's answer (requeued_at) is
// the one signal that they want it run again — the server's own rule.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

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
const ZR = (lk) => `https://www.ziprecruiter.com/jobs-search?search=marketing&lk=${lk}`;

(async () => {
  const fn = extract(BG, "async function forgetHandedBackFromApplied() {");
  check("forgetHandedBackFromApplied found", !!fn);
  check("buildAtsQueue calls it before reading the applied sets",
    /if \(typeof forgetHandedBackFromApplied === "function"\) await forgetHandedBackFromApplied\(\);\s*\n\s*const dd = await chrome\.storage\.local\.get\(\["appliedUrls", "appliedJobKeys"\]\);/
      .test(extract(BG, "async function buildAtsQueue(platform, perPlatformCap) {") || ""));
  check("a hand-back itself no longer touches the marks (that let a hand-finished job be re-applied)",
    !/forgetAppliedJob/.test(SRC));

  const run = async (handbacks, store) => {
    const ctx = {
      URL,
      apiGet: async () => { if (handbacks instanceof Error) throw handbacks; return { handbacks }; },
      chrome: { storage: { local: {
        get: async (keys) => Object.fromEntries(keys.map((k) => [k, store[k]])),
        set: async (o) => { Object.assign(store, o); },
      } } },
    };
    vm.createContext(ctx);
    vm.runInContext(`${fn}\n${extract(BG, "async function releaseAppliedMarks(rows) {")}\nglobalThis.f = forgetHandedBackFromApplied;`, ctx);
    await ctx.f();
    return store;
  };

  {
    const s = await run([{ url: GH, job_title: "Senior  Engineer", company: "Twilio", requeued_at: "2026-10-02T10:00:00Z" }],
      { appliedUrls: [`${GH}?gh_src=abc`, "https://x.io/a"], appliedJobKeys: ["senior engineer|twilio", "a|x"] });
    check("an ANSWERED hand-back is released (URL with its query, and its key)",
      s.appliedUrls.join() === "https://x.io/a" && s.appliedJobKeys.join() === "a|x", JSON.stringify(s));
  }
  {
    const s = await run([{ url: GH, job_title: "Senior Engineer", company: "Twilio", requeued_at: null }],
      { appliedUrls: [GH], appliedJobKeys: ["senior engineer|twilio"] });
    check("an UNANSWERED hand-back stays marked — it may have been finished by hand",
      s.appliedUrls.length === 1 && s.appliedJobKeys.length === 1, JSON.stringify(s));
  }
  {
    const s = await run([{ url: ZR("aaa"), job_title: "Cashier", company: "Shop", requeued_at: "2026-10-02T10:00:00Z" }],
      { appliedUrls: [ZR("aaa"), ZR("bbb"), ZR("ccc")], appliedJobKeys: [] });
    check("ZipRecruiter: only that exact posting, not every jobs-search URL",
      s.appliedUrls.join() === [ZR("bbb"), ZR("ccc")].join(), JSON.stringify(s.appliedUrls));
  }
  {
    const keep = { appliedUrls: [GH], appliedJobKeys: ["senior engineer|twilio"] };
    await run(new Error("503"), keep);
    check("hand-backs unreadable → the sets are left alone", keep.appliedUrls.length === 1 && keep.appliedJobKeys.length === 1);
  }

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
