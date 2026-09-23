"""Night-shift MVP: server-side Greenhouse apply, one job at a time.

WHY THIS EXISTS (decision 2026-09-21..23): the extension can only apply while the
user's machine is awake — measured 09-23: an "overnight" browser run found 3 discovered
jobs, finished in seconds, and a closed lid kills everything (hardware). Overnight
submission is only possible from a server walking the WHOLE pool. This is that
executor's first slice: pick → fill → (dry-run screenshot | live submit) → record.

ARCHITECTURE (recon 09-23, docs/handoff/night-shift.md): Greenhouse runs reCAPTCHA
Enterprise in SCORE mode (`enterprise.execute(key, {action:"apply_to_job"})`) — no human
challenge exists; a real (headless) browser obtains the token itself. The submit payload
carries csrf + fingerprint + jobApplicationRequestToken, so a raw HTTP POST is both
brittle and loud. Hence Playwright: the page assembles its own truth.

RULES BAKED IN:
  * dry-run is the default — fills everything, saves a screenshot, NEVER clicks Submit.
    Live mode is an explicit --live flag (Igor approved testing on his account 09-23).
  * resume = the user's default_resume dial via resume_storage.best_signed_url, never
    blindly the raw original (memory: resume-structure-authority).
  * screener answers = modules.ai_question_answer (adapt-not-invent rules live there).
  * every submit is recorded exactly like the extension's (applications row +
    mark_applied_by_link + activity line) so History/caps/dedup see ONE world.

Usage (from jobflow/, venv active, SUPABASE_* + ANTHROPIC_API_KEY exported):
  python scripts/night_shift/executor.py --user <uuid> [--job-url URL] [--live] [--headful]
"""

import argparse
import asyncio
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from app.db import applications as apps_db  # noqa: E402
from app.db import jobs as jobs_db  # noqa: E402
from app.db import resume as resume_storage  # noqa: E402
from app.db.client import get_supabase  # noqa: E402
from app.db.profile import get_profile  # noqa: E402
from modules.ai_fit_judge import assess_fit  # noqa: E402
from modules.ai_question_answer import answer_screener_question  # noqa: E402

SHOTS = os.path.join(os.path.dirname(__file__), "shots")

# A field the answerer refuses (returns "") stays EMPTY and, in live mode, blocks the
# submit — better a hand-back than an invented visa status (memory: adapt-not-invent).
REQUIRED_EMPTY_IS_FATAL = True


def log(msg: str) -> None:
    print(msg, flush=True)


def pick_jobs(user_id: str, job_url: str | None) -> list[dict]:
    """Candidates, best first. The walk tries them in order until one has a live form —
    dry-run #2 hit a job GH had already closed (`?error=true` redirect, the same shape
    that once stalled the auto-ATS walk on reddit?error=true) and a single-pick run
    just ended there."""
    sb = get_supabase()
    q = sb.table("jobs").select("*").eq("user_id", user_id).eq("platform", "greenhouse")
    q = q.eq("link", job_url) if job_url else q.eq("status", "new")
    rows = q.limit(60).execute().data or []
    if not rows:
        raise SystemExit("no matching greenhouse job in this user's pool")
    # Prefer GH-HOSTED apply pages (job-boards.greenhouse.io — the form lives right on
    # the page). Old-style boards.greenhouse.io links often redirect to a company's
    # CUSTOM careers site with no form at all — dry-run #1 landed on Cloudflare's
    # marketing page and "filled" its search box. Those rows are still walkable later
    # via a per-company adapter; today they are skipped, not guessed at.
    # FRESHNESS BEFORE SCORE: dry-run #3 walked the twelve best-scored rows and every
    # one was already closed (`?error=true`) — high score correlates with AGE in this
    # pool (median 11 days), so score-first ordering serves corpses first. A fresh row
    # is at least likely to exist; the fit judge decides quality later anyway.
    rows.sort(
        key=lambda r: (
            "job-boards.greenhouse.io" in (r.get("link") or ""),
            (r.get("date_found") or r.get("created_at") or ""),
            r.get("score") or 0,
        ),
        reverse=True,
    )
    return rows


def download_resume(user_id: str, profile: dict) -> str:
    url = resume_storage.best_signed_url(
        user_id,
        profile.get("ats_approved") or False,
        default_resume=profile.get("default_resume"),
    )
    if not url:
        raise SystemExit("no resume available for this user — refusing to apply without one")
    pdf = httpx.get(url, timeout=30)
    pdf.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".pdf", prefix="hd-resume-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(pdf.content)
    return path


async def find_application_form(page):
    """The actual application form, or None. NEVER fill outside it — dry-run #1 typed
    an AI answer into a careers-site SEARCH BOX because the pool link redirected to a
    custom landing page with no form. No form = skip the job, not improvise."""
    for sel in ("form:has(#first_name)", "form:has(input[type=file])", "#application-form"):
        loc = page.locator(sel)
        if await loc.count():
            return loc.first
    return None


async def fill_greenhouse(page, form, profile: dict, job: dict, resume_path: str) -> list[str]:
    """Fill the Greenhouse application form (scope = the form only)."""
    name = (profile.get("name") or "").strip()
    last = (profile.get("last_name") or "").strip()
    email = (profile.get("email") or "").strip()
    phone = (profile.get("phone") or "").strip()

    async def fill_if(sel: str, value: str) -> None:
        if not value:
            return
        el = form.locator(sel)
        if await el.count():
            await el.first.fill(value)

    await fill_if("#first_name", name)
    await fill_if("#last_name", last)
    await fill_if("#email", email)
    await fill_if("#phone", phone)

    # Resume: the visible control is a button, the real input is type=file.
    file_inputs = form.locator('input[type="file"]')
    if await file_inputs.count():
        await file_inputs.first.set_input_files(resume_path)
        # Greenhouse parses the upload (autofill) — give it a beat, then RE-ASSERT the
        # identity fields: autofill happily overwrites them with whatever it parsed.
        await page.wait_for_timeout(4000)
        await fill_if("#first_name", name)
        await fill_if("#last_name", last)
        await fill_if("#email", email)
        await fill_if("#phone", phone)

    # Custom questions: every labelled control that is still empty — INSIDE the form.
    unfilled: list[str] = []
    controls = form.locator(
        "input:not([type=file]):not([type=hidden]):not([type=search]), textarea, select"
    )
    n = await controls.count()
    for i in range(n):
        el = controls.nth(i)
        try:
            if not await el.is_visible():
                continue
            tag = (await el.evaluate("e => e.tagName")).lower()
            typ = ((await el.get_attribute("type")) or "").lower()
            cur = (
                (await el.input_value()) if tag != "select" else (await el.evaluate("e => e.value"))
            )
            if cur:
                continue
            label = await el.evaluate(
                "e => (e.labels && e.labels[0] && e.labels[0].textContent)"
                " || e.getAttribute('aria-label') || e.name || e.id || ''"
            )
            label = re.sub(r"\s+", " ", label or "").replace("*", "").strip()
            if not label or label.lower() in ("first name", "last name", "email", "phone"):
                continue
            required = await el.evaluate(
                "e => e.required || e.getAttribute('aria-required') === 'true'"
            )
            options: list[str] = []
            if tag == "select":
                options = await el.evaluate(
                    "e => [...e.options].map(o => o.textContent.trim())"
                    ".filter(t => t && !/^select/i.test(t))"
                )
            answer = answer_screener_question(
                label,
                job={
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "description": job.get("description") or "",
                },
                profile=profile,
                options=options or None,
            )
            if not answer:
                if required:
                    unfilled.append(label)
                continue
            if tag == "select":
                await el.select_option(label=answer)
            elif typ in ("checkbox", "radio"):
                continue  # GH custom radios are rare; leave for the hand-back list
            else:
                await el.fill(answer)
            log(f"  · {label[:60]} → {answer[:60]}")
        except Exception as exc:  # noqa: BLE001 — one odd widget must not kill the walk
            log(f"  ! field #{i} failed: {exc}")
    return unfilled


async def run(user_id: str, job_url: str | None, live: bool, headful: bool) -> None:
    profile = get_profile(user_id)
    # Email lives in Supabase auth, not profiles (same gap content.js patches from the JWT).
    if not profile.get("email"):
        admin = get_supabase().auth.admin.get_user_by_id(user_id)
        profile["email"] = admin.user.email if admin and admin.user else ""
    candidates = pick_jobs(user_id, job_url)
    resume_path = download_resume(user_id, profile)
    log(f"resume: {resume_path} | candidates: {len(candidates)}")

    os.makedirs(SHOTS, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headful)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        job = form = None
        for cand in candidates[:12]:  # bounded walk — this is a one-job MVP, not a sweep
            log(f"try: {cand['title']} @ {cand.get('company', '?')}\n     {cand['link']}")
            try:
                await page.goto(cand["link"], wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001 — a dead host is a skip, not a crash
                log(f"  skip (nav failed: {exc})")
                continue
            await page.wait_for_timeout(3000)
            form = await find_application_form(page)
            if form is None:
                log(f"  skip (no application form on {page.url})")
                continue
            # The same fit bar the extension applies — the SERVER side of one engine.
            # A live submit that skips the judge would send the LCSW postings this
            # user's résumé can never pass (dry-run #4 landed on exactly one).
            verdict = assess_fit(job=cand, profile=profile)
            if verdict.get("decision") != "apply":
                log(
                    f"  skip (fit {verdict.get('fit_score')}):"
                    f" {str(verdict.get('reason') or '')[:80]}"
                )
                continue
            log(f"  fit {verdict.get('fit_score')} — proceeding")
            job = cand
            break
        if job is None:
            log("no candidate with a live application form in this batch — nothing to do")
            await browser.close()
            return

        unfilled = await fill_greenhouse(page, form, profile, job, resume_path)

        shot = os.path.join(SHOTS, f"{job['id']}-filled.png")
        await page.screenshot(path=shot, full_page=True)
        log(f"screenshot: {shot}")

        if unfilled:
            log(f"required-but-empty: {unfilled}")
            if live and REQUIRED_EMPTY_IS_FATAL:
                log(
                    "LIVE ABORTED — a required field has no honest answer (hand-back, not a guess)."
                )
                await browser.close()
                return

        if not live:
            log("dry-run complete — nothing submitted.")
            await browser.close()
            return

        submit = form.locator('button[type="submit"], button:has-text("Submit application")')
        if not await submit.count():
            log("LIVE ABORTED — no submit button found")
            await browser.close()
            return
        await submit.first.click()
        try:
            await page.wait_for_url(re.compile(r"confirmation|thank"), timeout=30000)
            confirmed = True
        except Exception:  # noqa: BLE001
            await page.wait_for_timeout(8000)
            body = (await page.inner_text("body")).lower()
            confirmed = ("thank" in body) or ("received" in body) or ("submitted" in body)
        await page.screenshot(
            path=os.path.join(SHOTS, f"{job['id']}-after-submit.png"), full_page=True
        )
        log(f"submit outcome: {'CONFIRMED' if confirmed else 'UNCONFIRMED'} — {page.url}")

        status = "applied" if confirmed else "applied_unconfirmed"
        jobs_db.mark_applied_by_link(user_id, job["link"], status)
        apps_db.save_application(
            user_id=user_id,
            job_id=job["id"],
            cover_letter="",
            status=status,
            job_title=job["title"],
            company=job.get("company") or "",
            platform="greenhouse",
            job_url=job["link"],
        )
        get_supabase().table("activity_log").insert(
            {
                "user_id": user_id,
                "level": "info",
                "phase": "night-shift",
                "message": (
                    f"🌙 Night shift applied (server): {job['title']}"
                    f" @ {job.get('company', '?')} [{status}]"
                ),
            }
        ).execute()
        log("recorded: applications + jobs status + activity line")
        await browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--job-url")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--headful", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args.user, args.job_url, args.live, args.headful))
