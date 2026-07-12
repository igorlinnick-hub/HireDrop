# HireDrop — Platform Automation Plan

_Last updated: 2026-07-11. Owner: extension/backend. Connect flow for all 7 platforms is LIVE (validated 7/7 on a real account set). This doc maps each platform's apply mechanism to the automation we build for it, and the build order._

## Resume-tailoring economics (2026-07-11)

**Problem:** tailoring runs EAGER at "Find Jobs" — `jobs.py` tailors every score-≥N discovered job (tailored text + per-job ATS PDF, ~$0.028 each). But the tailor-set ≫ the apply-set, so most tailored PDFs are never used:
- Discovery platforms (LinkedIn/Glassdoor/Google/Wellfound/RemoteOK) = apply-manually → most never submitted.
- Indeed isn't in the tailor-set at all (not server-scraped).
- ZR: re-filtered by the extension's fit-gate (M1) at apply, most jobs are external Close-only (skipped), daily cap 50.
- Stop-campaign / user changes mind → tailoring already spent.
→ Estimated **~70%+ of tailoring spend is on resumes no application ever uses.**

**Done now (one-liners):**
- **#1 Tier-gate** — tailoring is Premium's differentiator (already stated in `subscriptions.py`); now enforced: `_tailor_allowed = tier in ("premium","admin")`. Free/pro no longer burn tailoring. `jobs.py`.
- **#3 Threshold by Apply Mode** — `_tailor_threshold = {broad:6, standard:7, precise:8}[apply_mode]` (was hardcoded 7). `jobs.py`.

**#2 Lazy tailoring — DONE (2026-07-11, econ pass). The ~70% win.**
Implemented WITHOUT any extension change: `GET /profile/resume/url/best?job_url=…` — which the extension already calls at apply time (`background.js:550`) — now tailors the matched job on demand the first time its resume is fetched, via `_lazy_tailor_for_job()` in `app/routers/profile.py`. Idempotent (skips if `tailored_resume` already present), keeps the #1 tier-gate (Premium/admin) + #3 Apply-Mode threshold. Eager tailoring was removed from `jobs.py:find_jobs` (scoring + save remain). Net: pay ~$0.028/tailor only for jobs that reach a real submission, not every score-≥N job discovered.
- **Remaining gap (frontend, NOT blocking):** discovery-platform *manual* applies no longer auto-tailor, and the dashboard job row won't show a tailored preview until applied. Fix later with a "Tailor for this job" button that calls the same lazy path on demand — those manual jobs were the bulk of the ~70% waste anyway.
- **#4 prompt-cache — DROPPED (won't help here).** Tailor cost is ~80% OUTPUT (1500 tok × $15/M); caching only discounts input. Cacheable blocks (~300–900 tok) sit below Sonnet's 1024-tok cache minimum, and #4 is anti-synergistic with #2 (caching needs a batch to reuse; lazy tailoring is one job at a time). Real per-call lever if ever needed: lower `max_tokens` or tailor with Haiku (~60% cheaper output) — a quality-vs-cost decision, not a free win.

## Daily caps + throughput (economics review 2026-07-11)

**DECISION — per-platform cap 50 → 20/day.** Bans are counted PER platform, so 20/day/platform keeps each account human-looking. Done in `app/db/subscriptions.py` (`MAX_PER_PLATFORM=20`). **TODO (extension workstream): align `chrome-extension/content.js:16` `MAX_APPLICATIONS_PER_PLATFORM` 50 → 20.** Until aligned, the extension will try up to 50 and the backend 429s at 20 (wasteful, not harmful).

**Still open — TOTAL daily cap (economics, separate from ban-safety).** 20/platform ≠ profit protection: with premium's 50/day total across platforms, a maxed user = 50×$0.03 = ~$45/mo > $29. Recommend `TIER_LIMITS["premium"]` 50 → ~30/day total (break-even protected). Not yet changed — founder's call.

**Throughput levers to raise apps/day WITHOUT losing quality or ban-safety** (browser click-speed is fixed by anti-detect — don't touch it):
- **Lever A — hide AI latency under the human-paced fill (same-page, ~15-30% gain).** NOTE the architecture: jobs are a known queue (`pendingJobs`) but WITHOUT descriptions, and navigation is full-page-reload per job → cross-job N/N+1 pipelining is NOT feasible. Real wins are same-page: (1) **batch screener answers** — currently EACH open-ended screener = a separate sequential `ANSWER_QUESTION` Sonnet round-trip (content.js ~1721/1835); extract all questions → ONE batched call (~10-30s saved on multi-screener jobs, the biggest win); (2) fire cover-letter generation async right after fit-pass and `await` it only at the paste field, so its ~5-8s hides under the fill (currently blocking, line ~1095).
- **Lever B — scale by BREADTH, not speed.** Ban-safety is per-platform → 20/day × N platforms = 20N total, no single account flagged. This is the ban-safe path to 100+/day. Build more fillers (ZR done; Greenhouse/Lever next) + ATS providers (one recipe = thousands of companies).
- **Lever C — more unattended runtime (a TRADE-OFF, not savings).** Auto-solving captchas (CapSolver, Turnstile wired) removes the human-babysit bottleneck → longer unattended sessions → more apps/day. Does NOT lower per-app cost; it raises total volume (and cost) while cutting manual effort. Trades against the ban-safe/semi-auto pivot — founder's call.

## Where we are

| Platform | Connect ✓ | Discovery (server fetch) | Auto-apply | Mechanism |
|---|---|---|---|---|
| Indeed | ✅ live detect | ❌ by design (in-browser only, compliance) | ✅ shipped | Native Easy Apply modal |
| ZipRecruiter | ✅ live detect | ✅ (JobSpy) | ✅ built, needs live E2E | Native Quick Apply modal |
| Glassdoor | ✅ live detect | ✅ (JobSpy) | 🔜 | Mostly routes to Indeed Apply / external ATS |
| Wellfound | ✅ live detect | ✅ (custom) | 🔜 | Own apply modal (+ "note to founder") |
| Monster | ✅ live detect | ❌ no scraper yet | 🔜 | Profile-based quick apply (resume stored ✓) |
| CareerBuilder | ✅ live detect | ❌ no scraper yet | 🔜 | "Easy Apply" (same identity as Monster) |
| Dice | ✅ live detect | ❌ no scraper yet | 🔜 | "Easy Apply" for tech roles |
| Google Jobs / RemoteOK | n/a (public) | ✅ | — | Aggregators → external apply |

**Key architectural fact:** the filler core is already universal — `phase3_fillForm` + AI screener answering (Loop 4, shipped) + honest-answers (M3) + fit-gate (M1) are platform-agnostic. Per-platform work is only: (a) find the job list/detail/apply-button selectors, (b) phase detection for that site's navigation model, (c) login/anti-bot handling. ZipRecruiter took ~1 day using this recipe.

## The leverage insight

Boards with a **native 1-click modal** (Indeed, ZR, and likely Monster/CB/Dice) each need their own selector recipe — leverage 1:1.

Everything else (Glassdoor externals, Wellfound externals, Google Jobs, RemoteOK, and a large share of ALL boards' listings) funnels into a handful of **ATS providers**. One ATS recipe = thousands of companies, regardless of which board surfaced the job:

- **Greenhouse** (`boards.greenhouse.io`, `job-boards.greenhouse.io`) — single-page form, no account required. Easiest + most common. **Build first.**
- **Lever** (`jobs.lever.co`) — single-page form, no account. Second.
- **Ashby** (`jobs.ashbyhq.com`) — SPA form, no account. Third.
- **Workday** (`*.myworkdayjobs.com`) — multi-step, per-company account required. Hardest; build last.

This is Loop 4 completed to its logical end: extension content script on ATS domains, `detectPlatform()` extended, `phase3_fillForm` reused, resume upload + AI screeners as today.

## Build order

1. **ZipRecruiter live E2E** (now) — run a real campaign, fix what trips, ship. Proves the multi-platform dispatch works end-to-end.
2. **M4 Truth Layer** (parallel-friendly) — applied/skipped + reason + score in the campaign report. Council's pre-money requirement; becomes even more important as platforms multiply.
3. **Greenhouse filler** — new content-script scope + selector recipe. Success metric: an external job discovered on any board gets auto-applied via its Greenhouse posting.
4. **Lever filler** — same pattern.
5. **Glassdoor investigation** (cheap, timeboxed) — verify that GD "Easy Apply" lands on indeed.com apply flow (same parent company). If yes: GD auto-apply ≈ free (content.js already runs on indeed.com); just link GD discovery → Indeed apply URLs.
6. **Ashby filler.**
7. **Wellfound native** — own modal, SPA, anti-bot wall observed (device verification). Selector-test session required; medium effort; startup-niche payoff.
8. **Monster / CareerBuilder / Dice native quick applies** — one selector-test session each; shared identity (Monster+CB) may share some DOM.
9. **Workday** — last (multi-page + per-company accounts; needs credential UX we haven't designed).
10. **Monster/CB/Dice discovery scrapers** — only if listings coverage demands it; ATS filler matters more than more boards.

## Connect-flow guarantees (shipped 2026-07-10)

- Auth entry URLs **verified live** (deep signup links 404 — never guess): unified auth pages for Indeed/ZR/Glassdoor/Monster/Dice → one "Log in / Sign up ↗" button; distinct signup only for Wellfound (`/join`) and CareerBuilder (`mode=SignUp`).
- `connect.js` (detection-only content script, 5 domains) survives SPA/OAuth logins: polls 3 min after load, then re-checks on tab focus + URL change; writes only on status change.
- Login detection signals documented per platform in `connect.js` header; validated against real logged-in/logged-out sessions.
- Dashboard panel: uniform per-platform rows (brand monogram, badge, one/two buttons), live ✓ for all 7, extension-absent fallback message.
- Campaign pre-flight + in-campaign login-wall guard (pause → notify → auto-resume) for auto-apply platforms.

## Risks / notes

- **Anti-bot reputation:** never run automated probes (Playwright/curl) against job boards from a user's network — this session got Igor's home IP temporarily flagged by Glassdoor/Wellfound. Live DOM inspection must be manual-browsing-shaped or done from a disposable network.
- **Selector drift:** every native-board recipe needs the Phase 4.1 backend-served selectors treatment (`GET_SELECTORS`) so fixes ship without extension updates.
- **CWS review:** each new host permission must map to visible user value (connect status / auto-apply). connect.js kept automation-free on purpose.
