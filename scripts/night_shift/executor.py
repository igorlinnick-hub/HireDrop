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
import contextlib
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
from app.db.subscriptions import check_can_apply, increment_free_apps  # noqa: E402
from modules.ai_fit_judge import assess_fit  # noqa: E402
from modules.ai_question_answer import answer_screener_question  # noqa: E402
from modules.job_identity import job_identity, normalized_link  # noqa: E402

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
    rows = q.limit(200).execute().data or []
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


def already_applied(user_id: str, link: str) -> bool:
    """Has this user already applied to this posting, by the SERVER's record?

    Identity, not string equality: the same Greenhouse posting reaches the pool as
    `boards.greenhouse.io/x/jobs/123`, `job-boards.greenhouse.io/x/jobs/123?gh_jid=123`
    and with tracking params, so comparing URLs literally misses the duplicate that
    matters. job_identity() reduces all of them to one key — the same reduction
    /campaign/queue uses to stop the extension re-applying.
    """
    target = job_identity(link) or (normalized_link(link) or link)
    try:
        applied = apps_db.applied_job_urls(user_id)
    except Exception as exc:  # noqa: BLE001
        # Fail CLOSED: if we cannot tell whether this was already sent, do not send it.
        # A skipped job costs one slot; a duplicate costs the user's credibility with an
        # employer, which is the thing the product exists to protect.
        log(f"  ! dedup lookup failed ({exc}) — refusing to apply blind")
        return True
    return any((job_identity(url) or (normalized_link(url) or url)) == target for url in applied)


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
            # "Already answered?" is a different question for a tickbox than for a text
            # field. input_value() on a checkbox/radio returns its VALUE ("on", "Yes",
            # "I don't wish to answer") regardless of whether anyone ticked it — so the
            # `if cur: continue` below used to skip every consent box and every EEO radio
            # before their label was even read, and they never reached the hand-back list
            # either. A required unticked consent then passed the pre-submit gate.
            if typ in ("checkbox", "radio"):
                cur = "ticked" if await el.is_checked() else ""
            else:
                cur = (
                    (await el.input_value())
                    if tag != "select"
                    else (await el.evaluate("e => e.value"))
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
                # Never tick a box on the user's behalf from a generated answer: these are
                # consents, certifications and EEO declarations, where a wrong tick is a
                # false statement made in their name. Hand it back instead — and do it
                # honestly, which the old `continue` did not (it claimed the hand-back
                # list, then never appended to it).
                if required:
                    unfilled.append(label)
                continue
            else:
                await el.fill(answer)
            log(f"  · {label[:60]} → {answer[:60]}")
        except Exception as exc:  # noqa: BLE001 — one odd widget must not kill the walk
            log(f"  ! field #{i} failed: {exc}")

    unfilled += await fill_comboboxes(page, form, profile, job)
    return unfilled


async def fill_comboboxes(page, form, profile: dict, job: dict) -> list[str]:
    """Greenhouse's required dropdowns are react-select comboboxes, not <select>.

    Live 09-23: the Zocdoc form's three required questions ("sub-department", "job
    level", "remote or hybrid") are rendered as `[role=combobox]` inputs. The <select>
    sweep above never saw them, they stayed empty, the submit was refused — and the run
    still recorded an application. This fills them the way content.js does: focus the
    combobox, read the options the menu renders, ask the answerer to pick one, click it.
    """
    missed: list[str] = []
    boxes = form.locator('[role="combobox"], [class*="select__control"] input')
    for i in range(await boxes.count()):
        box = boxes.nth(i)
        try:
            if not await box.is_visible():
                continue
            label = await box.evaluate(
                "e => { const w = e.closest('div[class*=select], .field, fieldset') || e.parentElement;"
                " const l = (e.labels && e.labels[0]) || (w && w.querySelector('label'));"
                " return (l && l.textContent) || e.getAttribute('aria-label') || ''; }"
            )
            label = re.sub(r"\s+", " ", label or "").replace("*", "").strip()
            if not label:
                continue
            # Already answered? react-select renders the choice as singleValue text.
            chosen = await box.evaluate(
                "e => { const w = e.closest('div[class*=select]');"
                " const v = w && w.querySelector('[class*=singleValue]');"
                " return v ? v.textContent.trim() : ''; }"
            )
            if chosen:
                continue
            await box.click()
            await page.wait_for_timeout(600)
            opts = page.locator('[class*="select__option"], [role="option"]')
            # Keep each option's REAL position in the menu next to its text. Filtering a
            # flat list and then clicking `opts.nth(texts.index(target))` clicks a
            # different option than the one chosen: drop the "Select one…" row and every
            # survivor shifts up by one, so "Onsite" picks "Hybrid" — and the log still
            # prints the answer we meant. Silent, and the employer gets an answer the
            # user never gave. Pairs make the position immune to filtering.
            pairs = [
                (j, re.sub(r"\s+", " ", (await opts.nth(j).inner_text()) or "").strip())
                for j in range(min(await opts.count(), 40))
            ]
            pairs = [(j, t) for j, t in pairs if t and not re.match(r"^select", t, re.I)]
            texts = [t for _, t in pairs]
            if not texts:
                missed.append(label)
                await page.keyboard.press("Escape")
                continue
            answer = answer_screener_question(
                label,
                job={
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "description": job.get("description") or "",
                },
                profile=profile,
                options=texts,
            )
            if not answer:
                missed.append(label)
                await page.keyboard.press("Escape")
                continue
            # The answerer returns one option verbatim; fall back to the closest match
            # rather than typing free text into a closed list.
            target = (
                answer
                if answer in texts
                else next((t for t in texts if answer.lower() in t.lower()), None)
            )
            if not target:
                missed.append(label)
                await page.keyboard.press("Escape")
                continue
            await opts.nth(pairs[texts.index(target)][0]).click()
            await page.wait_for_timeout(300)
            # Read back what the widget actually holds — clicking is not choosing, and a
            # log line that reports our intent instead of the field's state is how a wrong
            # answer reaches an employer looking correct in the transcript.
            settled = ""
            with contextlib.suppress(Exception):
                settled = await box.evaluate(
                    "e => { const w = e.closest('div[class*=select]');"
                    " const v = w && w.querySelector('[class*=singleValue]');"
                    " return v ? v.textContent.trim() : ''; }"
                )
            if settled and target.lower() not in settled.lower():
                missed.append(label)
                log(f"  ! {label[:50]}: wanted {target[:30]!r}, widget holds {settled[:30]!r}")
                continue
            log(f"  ▾ {label[:60]} → {settled or target}")
        except Exception as exc:  # noqa: BLE001
            log(f"  ! combobox #{i} failed: {exc}")
    return missed


class SubmitWatch:
    """Records the submit XHR's status code so a run can say WHY it failed.

    P2 of the lane. Greenhouse runs reCAPTCHA Enterprise in score mode, and the recon
    of 09-23 corrected the assumption this executor was built on: a low score does NOT
    make Greenhouse drop the application silently. Per their own docs it escalates —
    "a user may be asked to submit a code from their email" — and the submit answers
    428 while the page grows `#security-input-0..7` fields. That makes our reputation
    MEASURABLE from the run itself: no target board, no calibration key, no solver.

    So the run classifies four outcomes, not two, and the counter of `captcha_code`
    per hundred submits is the number that decides whether we ever pay for proxies.

    Deliberately a SYNC listener: it only reads `status`/`url` off the response, which
    needs no await. Reading the body would need one, and a handler that awaits inside
    a page event is how you deadlock a submit we only get to make once.
    """

    # Analytics and error reporting also POST during a submit; none of them are the form.
    _NOISE = re.compile(
        r"google-analytics|googletagmanager|segment|sentry|datadog|doubleclick|hotjar"
    )

    def __init__(self) -> None:
        self.status: int | None = None
        self.url: str = ""

    def attach(self, page) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        # Telemetry must never break a live submit — we only get to make it once.
        with contextlib.suppress(Exception):
            if response.request.method != "POST":
                return
            url = response.url
            if self._NOISE.search(url):
                return
            if not re.search(r"application|submit|apply", url, re.I):
                return
            # Keep the LAST matching POST: Greenhouse pre-flights the upload before the
            # real submit, and it is the final one that carries the verdict.
            self.status = response.status
            self.url = url


async def submit_outcome(page, form, watch: "SubmitWatch | None" = None) -> tuple[bool, str, str]:
    """(sent, why, outcome) — POSITIVE proof only.

    The employer either received it or did not; "probably" is not a state we may write
    to the database. Proof is one of: the board navigated to its confirmation URL, or
    the application form itself is gone from the page. Visible validation errors are
    proof of the opposite. Anything else is NOT SENT, on purpose.

    `outcome` is the P2 classification: sent | invalid | captcha_code | unknown. The
    order of the checks below is itself load-bearing — VALIDATION IS CHECKED FIRST,
    because our own unfilled-field bug (live 09-23, Zocdoc) produces a refusal that
    would otherwise be filed under "captcha", and a reputation metric poisoned by our
    own bugs is worse than no metric.
    """
    with contextlib.suppress(Exception):  # no navigation = the normal in-place GH submit
        await page.wait_for_url(re.compile(r"confirmation|thank|success"), timeout=25000)
        return True, "confirmation url", "sent"
    await page.wait_for_timeout(6000)

    errors = page.locator(
        '[class*="error"]:visible, [role="alert"]:visible, text=/This field is required/i'
    )
    try:
        n_err = await errors.count()
    except Exception:  # noqa: BLE001
        n_err = 0
    if n_err:
        try:
            first = re.sub(r"\s+", " ", (await errors.first.inner_text()) or "").strip()[:60]
        except Exception:  # noqa: BLE001
            first = ""
        return False, f"form refused it: {n_err} validation error(s) — {first}", "invalid"

    # Only now: the email-verification escalation. Either the page grew the code boxes
    # or the submit XHR answered 428 — both mean "Greenhouse wants a human here".
    code_ui = 0
    with contextlib.suppress(Exception):
        code_ui = await page.locator(
            '#security-input-0, [id^="security-input"], input[name*="security_code"]'
        ).count()
    if code_ui or (watch and watch.status == 428):
        return (
            False,
            f"Greenhouse asked for an emailed verification code (score too low; http {watch.status if watch else '?'})",
            "captcha_code",
        )

    # "The form vanished" is necessary but NOT sufficient. A posting that closed between
    # page load and submit answers non-2xx and redirects to the board root (?error=true) —
    # the form is gone there too, and the old code filed that as a sent application. So a
    # vanished form only counts when the submit XHR we watched did not say otherwise.
    http_ok = watch is None or watch.status is None or 200 <= watch.status < 300
    try:
        gone = await form.count() == 0 or not await form.is_visible()
    except Exception:  # noqa: BLE001 — a detached form means the document moved on
        gone = True
    if gone and http_ok:
        return True, f"form gone after submit (http {watch.status if watch else 'n/a'})", "sent"
    if gone:
        return (
            False,
            f"form gone but the board answered http {watch.status} — not a submission",
            "unknown",
        )

    body = (await page.inner_text("body")).lower()
    if "your application" in body and ("received" in body or "submitted" in body):
        return True, "confirmation text", "sent"
    return (
        False,
        f"no proof of sending (form still on screen, http {watch.status if watch else '?'})",
        "unknown",
    )


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
        for cand in candidates[:40]:  # bounded walk — this is a one-job MVP, not a sweep
            log(f"try: {cand['title']} @ {cand.get('company', '?')}\n     {cand['link']}")
            try:
                await page.goto(cand["link"], wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001 — a dead host is a skip, not a crash
                log(f"  skip (nav failed: {exc})")
                continue
            await page.wait_for_timeout(3000)
            form = await find_application_form(page)
            if form is None:
                # GH answers a closed posting with a redirect to the board root
                # (`?error=true`) — bury the row so no run re-opens this corpse. Live
                # 09-23: the twelve freshest candidates were ALL dead; without burial
                # every future walk pays the same twelve page-loads first. Only in
                # auto-pick mode: an explicit --job-url misfire must not retire a row.
                if not job_url and ("error=true" in page.url or "/jobs/" not in page.url):
                    n = jobs_db.mark_dead_link(user_id, cand["link"])
                    log(f"  skip (dead posting — retired {n} pool row(s))")
                else:
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
            # ALREADY APPLIED? The pool row's own status is not the answer. The same
            # posting arrives under several spellings (harvest vs browser query strings),
            # so a row created AFTER an application still reads `new`; and --job-url
            # bypasses the status filter entirely, which makes a re-run — a retry after a
            # crash, a repeated test — send the employer a SECOND real application.
            # The server already knows better: applied_job_urls + job_identity is the
            # same answer /campaign/queue uses to keep the extension honest.
            if already_applied(user_id, cand["link"]):
                log("  skip (already applied to this posting — server's own record)")
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

        # THE CAP IS THE BACKEND'S, AND THIS PATH HAS TO ASK IT TOO.
        # Until now the night shift wrote `applications` rows straight through the
        # service_role client, so the only real gate in the product — check_can_apply
        # behind POST /applications/save, which counts today's rows and 429s the 31st —
        # never saw a server-side submit. One writer obeyed the 30/day, 15/platform,
        # free-taste and expired-subscription limits; the other did not know they existed.
        # Asked HERE, immediately before the click: the answer must be as fresh as
        # possible, since the extension may have been applying on the user's machine
        # while this walk was reading forms.
        gate = check_can_apply(user_id, "greenhouse", profile.get("email"))
        if not gate.get("allowed"):
            log(f"LIVE ABORTED — cap reached: {gate.get('reason')}")
            with contextlib.suppress(Exception):
                get_supabase().table("activity_log").insert(
                    {
                        "user_id": user_id,
                        "level": "info",
                        "phase": "night-shift",
                        "message": (
                            f"🌙 Night shift stood down [outcome=capped]: {gate.get('reason')}"
                            f" ({gate.get('used_today')}/{gate.get('daily_limit')} today)."
                        ),
                    }
                ).execute()
            await browser.close()
            return
        watch = SubmitWatch()
        watch.attach(page)  # armed BEFORE the click — the verdict rides the submit XHR
        await submit.first.click()
        confirmed, why, outcome = await submit_outcome(page, form, watch)
        await page.screenshot(
            path=os.path.join(SHOTS, f"{job['id']}-after-submit.png"), full_page=True
        )
        log(f"submit outcome: {outcome.upper()} — {why} | http {watch.status} | {page.url}")

        if not confirmed:
            # NOT SENT is not a weaker kind of applied — it is a hand-back. Live 09-23
            # recorded a Zocdoc application the employer never received (three required
            # comboboxes were empty, the page simply re-rendered with validation errors)
            # because the old detector matched the word "submitted" in the button label.
            # A row written without proof of sending makes silent failure look like work.
            get_supabase().table("activity_log").insert(
                {
                    "user_id": user_id,
                    "level": "warn",
                    "phase": "night-shift",
                    # The outcome word is machine-countable on purpose: `outcome=captcha_code`
                    # per hundred submits IS the reputation metric (P2), and `outcome=invalid`
                    # separates our own filling bugs from Greenhouse's judgement of us.
                    "message": (
                        f"🌙 Night shift could NOT send [outcome={outcome} http={watch.status}]:"
                        f" {job['title']} @ {job.get('company', '?')} — {why}."
                        " Nothing recorded as applied."
                    ),
                }
            ).execute()
            await browser.close()
            return

        status = "applied"
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
        # The lifetime free-taste counter lives beside the daily cap and is advanced by
        # POST /applications/save, which this path bypasses. Without this a free user's
        # night submits would never count toward FREE_APP_LIMIT — the gate would stay
        # open forever for exactly the applications nobody is watching.
        if gate.get("tier") == "free":
            with contextlib.suppress(Exception):
                increment_free_apps(user_id)
        get_supabase().table("activity_log").insert(
            {
                "user_id": user_id,
                "level": "info",
                "phase": "night-shift",
                "message": (
                    f"🌙 Night shift applied (server) [outcome=sent http={watch.status}]:"
                    f" {job['title']} @ {job.get('company', '?')} [{status}]"
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
