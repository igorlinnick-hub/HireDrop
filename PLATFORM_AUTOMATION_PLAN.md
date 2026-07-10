# HireDrop — Platform Automation Plan

_Last updated: 2026-07-10. Owner: extension/backend. Connect flow for all 7 platforms is LIVE (validated 7/7 on a real account set). This doc maps each platform's apply mechanism to the automation we build for it, and the build order._

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
