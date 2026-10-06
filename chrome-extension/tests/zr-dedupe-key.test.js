// Two ZipRecruiter postings on one results page must be two jobs. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/zr-dedupe-key.test.js
//
// A ZR posting is the results page plus `?lk=<uuid>`. The detail phase keyed its session
// dedup by the URL path, so every posting on /jobs-search/2 shared one key: the first was
// processed, the rest skipped as "already processed". The list phase meanwhile filters
// cards by the uuid — a key the detail phase never wrote. The URL below is the one ZR row
// in applications (09-06), verbatim.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}
function fn(name) {
  const a = SRC.indexOf(`  function ${name}(`);
  const b = SRC.indexOf("\n  }\n", a);
  if (a < 0 || b < 0) {
    console.error(`function moved or renamed: ${name}`);
    process.exit(2);
  }
  return SRC.slice(a, b + 4);
}

const ctx = vm.createContext({ URL, String });
vm.runInContext(fn("jobIdFromUrl") + fn("zrDedupeKey") + "\nthis.zrDedupeKey = zrDedupeKey;", ctx);

const REAL =
  "https://www.ziprecruiter.com/jobs-search/2?employment_type%5B%5D=full_time&location=Miami%2C+Florida%2C+US&search=social+media+manager&lk=PpfY8jOjIWgxM4IiHjAsag";
const NEIGHBOUR = REAL.replace("PpfY8jOjIWgxM4IiHjAsag", "Zq0bXr4NnS2yW1tVvLmAeQ");

check("key is the posting's lk uuid, not the results-page path",
  ctx.zrDedupeKey(REAL) === "PpfY8jOjIWgxM4IiHjAsag", ctx.zrDedupeKey(REAL));
check("two postings on one results page get two keys",
  ctx.zrDedupeKey(REAL) !== ctx.zrDedupeKey(NEIGHBOUR));
check("same posting reached with a different search query is still one key",
  ctx.zrDedupeKey("https://www.ziprecruiter.com/jobs-search?search=x&lk=PpfY8jOjIWgxM4IiHjAsag") ===
    ctx.zrDedupeKey(REAL));
check("no lk (a /c/<co>/job/ page) falls back to the path",
  ctx.zrDedupeKey("https://www.ziprecruiter.com/c/Acme/Job/Designer?jid=1") ===
    "https://www.ziprecruiter.com/c/Acme/Job/Designer");

// The detail phase must use it, and the list phase must filter by the same uuid.
const detail = SRC.slice(SRC.indexOf("ZipRecruiter job detail — waiting for right panel"));
check("ZR detail phase dedups with zrDedupeKey",
  /const dedupeKey = zrDedupeKey\(jobUrl\);/.test(detail.slice(0, 4000)));
check("ZR list phase filters cards by the same uuid", /processedKeys\.has\(uuid\)/.test(SRC));

if (failures) {
  console.error(`\n${failures} failing`);
  process.exit(1);
}
console.log("\nall ok");
