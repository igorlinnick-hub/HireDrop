// LinkedIn capture kit (content.js CAPTURE KIT block): the saved page must not carry the
// person who captured it.
//
//   node <repo>/jobflow/chrome-extension/tests/linkedin-capture.test.js
//
// The page below is a SYNTHETIC GENERIC PAGE written for this test — it is NOT a LinkedIn
// fixture and makes no claim about LinkedIn's DOM. Repo rule: fixtures of a real site are
// captured from the real site (with this kit), never invented. What it pins is the kit's own
// contract on any DOM: emails, phones, typed input values, script bodies, JSON data blobs,
// the member's name and profile links leave; the structure and the job ids stay.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${String(detail || "").slice(0, 300)})`}`);
}
function slice(from, to) {
  const a = SRC.indexOf(from);
  const b = SRC.indexOf(to, a);
  if (a < 0 || b < 0) {
    console.error(`markers moved: ${from} … ${to}`);
    process.exit(2);
  }
  return SRC.slice(a, b);
}

const CODE =
  slice("  function maskPii(s) {", "  function formBlockers() {") +
  slice("  // ---- CAPTURE KIT", "  // ---- END CAPTURE KIT");

const SYNTHETIC_GENERIC_PAGE = `<!DOCTYPE html><html><head>
  <meta name="csrf-token" content="ajax:0123456789abcdef">
  <title>Synthetic generic page</title>
  <style>.x{box-shadow:0 0 0 1px #000}</style>
  <script>window.__secret = {"email":"jane.doe@example.com","token":"abc"};</script>
  <script type="application/ld+json">{"name":"Jane Doe"}</script>
</head><body>
  <header>
    <div class="global-nav__me"><img class="global-nav__me-photo" alt="Jane Doe" src="https://img.example/profile-displayphoto-shrink_100_100/abc">
      <span>Jane Doe</span><span>Senior Widget Engineer at Acme</span></div>
  </header>
  <main>
    <ul>
      <li data-occludable-job-id="4012345678"><a href="/jobs/view/4012345678/">Widget Engineer</a> <span>Easy Apply</span></li>
    </ul>
    <p>Contact jane.doe@example.com or call (808) 555-1234.</p>
    <p>Posted by <a href="https://social.example/in/jane-doe-12ab/">Jane</a> · urn:li:fsd_profile:ACoAAB1234xyz</p>
    <code style="display:none" id="bpr-guid-1">{"data":{"firstName":"Jane","lastName":"Doe","publicIdentifier":"jane-doe-12ab"}}</code>
    <code style="display:none" id="bpr-guid-2"><!--{"data":{"firstName":"Janet","publicIdentifier":"janet-secret-slug","emailAddress":"janet@corp.test"}}--></code>
    <!-- member: Janet Q, janet@corp.test -->
    <div data-csrf="ajax:feedfacecafe" data-test-x="1">csrf in an attribute</div>
    <div class="avatar" style="width:4px;background-image:url('https://media.example/profile-photo-of-janet.jpg')"></div>
    <span>urn:li:person:ABC123secret</span>
    <div role="dialog" aria-label="Easy Apply to Acme">
      <h3>Contact info</h3>
      <div role="progressbar" aria-valuenow="25"></div>
      <label>Email <input type="email" value="jane.doe@example.com"></label>
      <label>Phone <input type="tel" value="8085551234"></label>
      <label>Token <input type="hidden" name="csrf" value="ajax:0123456789abcdef"></label>
      <label><input type="radio" name="auth" value="Yes"> Yes</label>
      <textarea>My name is Jane Doe and I love widgets</textarea>
      <div contenteditable="true">Jane typed this</div>
      <select><option value="jane.doe@example.com">jane.doe@example.com</option></select>
      <button aria-label="Continue to next step">Next</button>
    </div>
    <div id="host"></div>
  </main>
</body></html>`;

const { window } = new JSDOM(SYNTHETIC_GENERIC_PAGE, { url: "https://synthetic.example/jobs/view/4012345678/" });
const doc = window.document;
// One open shadow root with PII inside, one closed (must not be readable, must not crash).
const open = doc.getElementById("host").attachShadow({ mode: "open" });
open.innerHTML = '<p class="in-shadow">Shadow says jane.doe@example.com</p>';
const closedHost = doc.createElement("div");
doc.querySelector("main").appendChild(closedHost);
closedHost.attachShadow({ mode: "closed" }).innerHTML = "<p>closed jane.doe@example.com</p>";

const ctx = vm.createContext({ document: doc, window });
vm.runInContext(
  CODE +
  "\nthis.terms = captureIdentityTerms; this.html = captureSanitizedHtml; this.kind = capturePageKind;" +
  " this.stamp = captureTimestamp;",
  ctx,
);

const profile = { name: "Jane Doe", email: "jane.doe@example.com", phone: "808-555-1234", linkedin_url: "https://www.linkedin.com/in/jane-doe-12ab/" };
const terms = ctx.terms(profile, doc);
check("identity terms include the full name", terms.includes("Jane Doe"), JSON.stringify(terms));
check("identity terms are longest first", terms.every((t, i) => i === 0 || terms[i - 1].length >= t.length), JSON.stringify(terms));
check("identity terms include the profile slug", terms.includes("jane-doe-12ab"), JSON.stringify(terms));

const before = doc.documentElement.outerHTML;
const out = ctx.html(doc, terms);
check("the live page is not modified", doc.documentElement.outerHTML === before);

check("no email anywhere", !/jane\.doe|@example\.com/i.test(out), out.match(/.{0,40}jane\.doe.{0,40}/i));
check("no phone digits", !/555[-\s]?1234|8085551234/.test(out), out.match(/.{0,40}555.{0,40}/));
check("no member name", !/\bJane\b|\bDoe\b/.test(out), out.match(/.{0,60}(Jane|Doe).{0,60}/));
check("no profile slug", !/jane-doe-12ab/.test(out), out.match(/.{0,40}jane-doe.{0,40}/));
check("member urn masked", /urn:li:fsd_profile:redacted/.test(out) && !/ACoAAB1234xyz/.test(out));
check("script bodies stripped", !/__secret|"token"/.test(out) && /<script><\/script>/.test(out));
check("JSON <code> blob stripped", !/firstName|publicIdentifier/.test(out));
check("csrf meta content stripped", !/0123456789abcdef/.test(out));
check("JSON inside an HTML comment in <code> stripped", !/janet-secret-slug|janet@corp|Janet/.test(out), out.match(/.{0,60}[Jj]anet.{0,60}/));
check("no HTML comments survive", !/<!--/.test(out), out.match(/<!--.{0,60}/));
check("csrf data attribute removed", !/feedfacecafe|data-csrf/.test(out) && /data-test-x="1"/.test(out));
check("inline background photo redacted, other style kept", !/profile-photo-of/.test(out) && /width:4px/.test(out));
check("person urn masked", /urn:li:person:redacted/.test(out) && !/ABC123secret/.test(out));
check("typed input values stripped", !/<input[^>]*type="(email|tel|hidden)"[^>]*value=/.test(out));
check("radio option value kept (not typed, fixture needs it)", /type="radio" name="auth" value="Yes"/.test(out));
check("textarea text stripped", !/love widgets/.test(out));
check("contenteditable text stripped", !/typed this/.test(out));
check("profile photo src stripped", !/profile-displayphoto/.test(out));
check("nav identity box words replaced", !/Senior Widget Engineer at Acme/.test(out));
check("job id kept in attribute", /data-occludable-job-id="4012345678"/.test(out));
check("job link kept", /href="\/jobs\/view\/4012345678\/"/.test(out));
check("CSS digits untouched", /0 0 0 1px/.test(out));
check("structure kept (dialog, button label)", /role="dialog"/.test(out) && /Continue to next step/.test(out));
check("open shadow root included as declarative template", /<template shadowrootmode="open">/.test(out) && /in-shadow/.test(out));
check("shadow content masked too", !/Shadow says jane/.test(out) && /Shadow says &lt;email&gt;|Shadow says <email>/.test(out), out.match(/Shadow says.{0,40}/));

// Page kind — URLs are plain strings; the DOM is the synthetic page above.
const memo = { states: [] };
check("modal open → modal-step-1", ctx.kind("https://synthetic.example/jobs/view/1/", doc, memo), "modal-step-1");
check("same step captured twice keeps its number", ctx.kind("https://synthetic.example/jobs/view/1/", doc, memo), "modal-step-1");
doc.querySelector("[role='progressbar']").setAttribute("aria-valuenow", "50");
doc.querySelector("[role='dialog'] h3").textContent = "Additional questions";
check("next step → modal-step-2", ctx.kind("https://synthetic.example/jobs/view/1/", doc, memo), "modal-step-2");
const sent = doc.createElement("p");
sent.textContent = "Your application was sent to Acme";
doc.querySelector("[role='dialog']").appendChild(sent);
check("'application was sent' → confirmation", ctx.kind("https://synthetic.example/jobs/view/1/", doc, memo), "confirmation");
doc.querySelector("[role='dialog']").remove();
const blank = new JSDOM("<body></body>").window.document;
check("search URL → search", ctx.kind("https://www.linkedin.com/jobs/search/?keywords=x", blank, memo), "search");
check("collections URL → search", ctx.kind("https://www.linkedin.com/jobs/collections/recommended/", blank, memo), "search");
check("view URL → view", ctx.kind("https://www.linkedin.com/jobs/view/4012345678/", blank, memo), "view");
check("anything else → other", ctx.kind("https://www.linkedin.com/feed/", blank, memo), "other");

const stamp = ctx.stamp(new Date("2026-10-05T12:34:56.789Z"));
check("timestamp is file-name safe", stamp === "2026-10-05T12-34-56Z", stamp);

// The kit refuses off LinkedIn / with the flag off — the handler gates on both.
const kit = slice("  async function captureLinkedInPage() {", "  // ---- END CAPTURE KIT");
check("handler refuses off LinkedIn", /detectPlatform\(\) !== "linkedin"\) return \{ ok: false/.test(kit));
check("handler refuses with the flag off", /linkedinBeta !== true\) return \{ ok: false, error: "flag_off" \}/.test(kit));
check("file name is linkedin-<kind>-<timestamp>.html", /`linkedin-\$\{kind\}-\$\{captureTimestamp\(new Date\(\)\)\}\.html`/.test(kit));

if (failures) {
  console.log(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
