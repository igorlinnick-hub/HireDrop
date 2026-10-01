"""Night shift: server-side apply on Greenhouse and Ashby, for when the user's machine sleeps.

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
                                         [--platform greenhouse|ashby|all] [--max N]

`--max N` is how many applications ONE run may send (default 1). The walk ends earlier
when the cap is reached or three submits in a row are refused (walk_verdict).
"""

import argparse
import asyncio
import contextlib
import os
import re
import sys
import tempfile
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import ashby  # noqa: E402
import httpx  # noqa: E402
from common import (  # noqa: E402
    _DECLINE_OPT,
    _DEMOGRAPHIC_Q,
    _NO_OPT,
    _OPT_IN_Q,
    is_knockout,
    log,
    pick_typeahead,
    same_value,
    typeahead_kind,
    typeahead_queries,
    walk_verdict,
)
from common import answer as night_answer  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from app.db import applications as apps_db  # noqa: E402
from app.db import handbacks as handbacks_db  # noqa: E402
from app.db import jobs as jobs_db  # noqa: E402
from app.db import resume as resume_storage  # noqa: E402
from app.db.client import get_supabase  # noqa: E402
from app.db.profile import get_profile  # noqa: E402
from app.db.subscriptions import check_can_apply, increment_free_apps  # noqa: E402
from app.routers.jobs import (  # noqa: E402
    PREJUDGE_CALLS,
    PREJUDGE_SECS,
    _ats_candidates,
    _prejudged_queue,
)
from modules.ai_cover_letter import (
    generate_cover_letter,  # noqa: E402
    resume_text_for,  # noqa: E402
)
from modules.ai_fit_judge import assess_fit, mode_threshold, verdict_version  # noqa: E402
from modules.fit_queue import has_current_verdict  # noqa: E402
from modules.job_identity import job_identity, normalized_link  # noqa: E402
from modules.job_location import (  # noqa: E402
    location_verdict,
    matches_work_setting,
    names_foreign_country,
    parse_user_location,
)

SHOTS = os.path.join(os.path.dirname(__file__), "shots")

# A field the answerer refuses (returns "") stays EMPTY and, in live mode, blocks the
# submit — better a hand-back than an invented visa status (memory: adapt-not-invent).
REQUIRED_EMPTY_IS_FATAL = True

PLATFORMS = ("greenhouse", "ashby")


def pick_jobs(
    user_id: str, job_url: str | None, profile: dict, platforms: tuple = PLATFORMS
) -> list[dict]:
    """Candidates, best first. The walk tries them in order until one has a live form —
    dry-run #2 hit a job GH had already closed (`?error=true` redirect, the same shape
    that once stalled the auto-ATS walk on reddit?error=true) and a single-pick run
    just ended there."""
    if job_url:
        # Igor's own pick: skips the search filter, but not the per-candidate gates.
        sb = get_supabase()
        q = sb.table("jobs").select("*").eq("user_id", user_id).in_("platform", list(platforms))
        rows = q.eq("link", job_url).limit(5).execute().data or []
    else:
        # ONE QUEUE FOR EVERY EXECUTOR. The night shift is the second executor of the
        # queue the extension walks by day — not a second opinion about it. So it reads
        # the pool through the SAME three steps as /jobs/ats-queue: the search filter +
        # age gate (_ats_candidates), then the prejudged queue (_prejudged_queue): rows
        # already judged below the user's bar are out without a model call, the rest go
        # freshest first, and the company cap (2 per 60 days, Igor 09-30) counts both
        # what was already sent and what this very list would send.
        # Until 09-30 this walk kept its own copy of those rules and judged every
        # candidate again, live: a dry walk lined up THREE DoorDash applications for one
        # night, and scored the same posting 38 on one pass and 42 on the next — a
        # different list from the one the user is shown.
        _pool, _on_search, live = _ats_candidates(user_id, profile)
        live = [r for r in live if r.get("platform") in platforms]
        # A job already handed back is WAITING ON THE PERSON. Without this the walk
        # re-opens the same form every night, fills it to the same wall and stops there
        # — live 09-30 the freshest fit was a DoorDash form two fields short, and it
        # would have been the first pick of every run after it.
        waiting = open_handback_keys(user_id)
        live = [r for r in live if _job_key(r.get("link") or "") not in waiting]
        queue = _prejudged_queue(
            user_id,
            profile,
            live,
            limit=len(live),
            judge_calls=PREJUDGE_CALLS,
            deadline_s=PREJUDGE_SECS,
        )
        rows = queue["jobs"]
        log(
            f"queue: {len(rows)} to walk ({queue['passing']} prejudged, {queue['unjudged']}"
            f" to judge at the form) | below the bar: {queue['below_bar']}"
            f" | over the company cap: {queue['company_capped']}"
        )
    if not rows:
        raise SystemExit(f"no matching {'/'.join(platforms)} job in this user's queue")
    # Board-hosted apply pages first (job-boards.greenhouse.io, jobs.ashbyhq.com — the
    # form lives right on the page). Old-style boards.greenhouse.io links often redirect
    # to a company's CUSTOM careers site with no form at all — dry-run #1 landed on
    # Cloudflare's marketing page and "filled" its search box. A STABLE partition: inside
    # each half the queue's own order (freshest first) is untouched.
    rows.sort(
        key=lambda r: any(
            h in (r.get("link") or "") for h in ("job-boards.greenhouse.io", "ashbyhq.com")
        ),
        reverse=True,
    )
    return rows


def _job_key(link: str) -> str:
    return job_identity(link) or (normalized_link(link) or link)


def open_handback_keys(user_id: str) -> set[str]:
    try:
        return {_job_key(h.get("url") or "") for h in handbacks_db.list_open(user_id, limit=100)}
    except Exception as exc:  # noqa: BLE001 — worst case we meet the same wall once more
        log(f"  ! hand-back lookup failed ({exc}) — walking without it")
        return set()


def record_handback(
    user_id: str,
    job: dict,
    platform: str,
    unfilled: list[str],
    reason: str = "",
    outcome: str = "handback",
) -> None:
    """A form the night shift could not finish is a JOB FOR THE PERSON, not a log line.

    Until 09-30 this path only printed and exited: nothing reached History, the dashboard
    never asked, and the 30-day hand-back measurement that decides which questions signup
    asks (modules/employer_answers.py) could not see a field the night shift kept dying
    on. Written exactly like the extension's hand-back — the durable row the dashboard
    asks from, plus the tagged activity line `handback_stats` counts.

    Two ways in: required fields with no honest answer (`unfilled`), and a submit the
    board did not accept (`reason`, with the board's own `outcome` word). Either way the
    open row also keeps the walk from re-opening the same form tomorrow night
    (pick_jobs) — which for a refused submit matters twice: Greenhouse's refusal comes
    with a verification code emailed to the user, and a blind retry is one more email.
    """
    labels = [str(q)[:300] for q in unfilled][:12]
    reason = reason or ("No answer on file for: " + ", ".join(labels[:6]))
    try:
        handbacks_db.add(
            user_id,
            {
                "job_title": job.get("title") or "",
                "company": job.get("company") or "",
                "url": job.get("link") or "",
                "platform": platform,
                "reason": reason,
                "questions": labels,
                "job_id": job.get("id"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        # Said out loud: a swallowed failure here is how the hand-back table sat empty
        # for six days (09-21 → 09-27) while every caller believed it was being written.
        log(f"  ! hand-back NOT recorded ({str(exc).splitlines()[0][:120]})")
    _note(
        user_id,
        "warn",
        f"🌙 Night shift needs your hands [outcome={outcome} board={platform}]:"
        f" {job.get('title')} @ {job.get('company', '?')} — {reason}.",
        {"type": "handback", "platform": platform, "unfilled": labels},
    )


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


async def knockout_answers(form) -> list[str]:
    """Questions whose own answer disqualifies this candidate, as the form now stands.

    A screener that asks "are you located in X / can you work from our office / do you
    have N years" is not a preference — it is the employer's filter, and answering "no"
    is an application they will reject on sight. We answer honestly (we never claim a
    location or a credential the résumé does not support), so the right move is to not
    spend the slot at all.

    Read from the widgets AFTER filling, not from our intent: what matters is what the
    employer would receive.
    """
    out: list[str] = []
    with contextlib.suppress(Exception):
        blocks = form.locator('div[class*="select"]:has([class*="singleValue"])')
        for i in range(min(await blocks.count(), 40)):
            blk = blocks.nth(i)
            pair = await blk.evaluate(
                "e => { const l = e.closest('div')&&e.closest('div').querySelector('label');"
                " const w = e.querySelector('[class*=singleValue]');"
                " let q = l ? l.textContent : '';"
                " if (!q) { let n = e; for (let h=0; h<5 && n; h++) {"
                "   const lab = n.querySelector && n.querySelector('label');"
                "   if (lab && lab.textContent.trim()) { q = lab.textContent; break; } n = n.parentElement; } }"
                " return [q||'', w ? w.textContent : '']; }"
            )
            question = re.sub(r"\s+", " ", (pair[0] or "")).replace("*", "").strip()
            answer = re.sub(r"\s+", " ", (pair[1] or "")).strip()
            if not question or not answer:
                continue
            if is_knockout(question, answer):
                out.append(f"{question[:90]} → {answer[:30]}")
    return out


async def find_application_form(page):
    """The actual application form, or None. NEVER fill outside it — dry-run #1 typed
    an AI answer into a careers-site SEARCH BOX because the pool link redirected to a
    custom landing page with no form. No form = skip the job, not improvise."""
    for sel in ("form:has(#first_name)", "form:has(input[type=file])", "#application-form"):
        loc = page.locator(sel)
        if await loc.count():
            return loc.first
    return None


async def fill_greenhouse(
    page, form, profile: dict, job: dict, resume_path: str
) -> tuple[list[str], str]:
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
    # react-select renders its search box as a plain <input type=text role=combobox>, so
    # this sweep used to type a free-text answer straight into a CLOSED LIST before
    # fill_comboboxes got there (live 09-25: "Sexual Orientation → This is a personal
    # question unrelated to my qualifications…" typed into a dropdown's search field).
    # Every such field was answered twice, paying for two AI calls and risking a
    # half-typed filter. Comboboxes belong to fill_comboboxes; leave them alone here.
    controls = form.locator(
        "input:not([type=file]):not([type=hidden]):not([type=search])"
        ':not([role="combobox"]):not([class*="select__input"]), textarea, select'
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
            answer = night_answer(
                label,
                {
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "description": job.get("description") or "",
                },
                profile,
                options or None,
                numeric=typ == "number",
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
                # A one-word answer is a datum, not a sentence: the model returned
                # "Igor." for Preferred First Name, and a trailing full stop inside a
                # name box is the kind of thing a recruiter reads as carelessness.
                if " " not in answer.strip():
                    answer = answer.strip().rstrip(".")
                await el.fill(answer)
            log(f"  · {label[:60]} → {answer[:60]}")
        except Exception as exc:  # noqa: BLE001 — one odd widget must not kill the walk
            log(f"  ! field #{i} failed: {exc}")

    unfilled += await fill_comboboxes(page, form, profile, job)
    letter = await fill_cover_letter(page, form, profile, job)
    return unfilled, letter


async def fill_cover_letter(page, form, profile: dict, job: dict) -> str:
    """Write the letter INTO the form, and only if the form asks for one. Returns the
    text actually typed, or "" when this posting has no cover-letter field.

    Lazy on purpose (the product rule from PR #240): no field ⇒ no model call, no text
    recorded. Measured 09-23, the letter used to be written for nearly every application
    and reached a form once in 274 attempts — paying for prose nobody read.

    The Greenhouse traps below are not guesses; they cost PR #248 a full lane:
      * the field is hidden behind an "Enter manually" trigger, `data-testid`
        `cover_letter-text`;
      * walking up with closest("div") finds only the button's own wrapper, whose text
        is "Enter manually" — so the block test fails and every real trigger is rejected
        (six live forms reported "no cover-letter field");
      * the revealed textarea is `id=cover_letter_text` with NO name, and its label also
        reads "Enter manually", so it can only be found by id or by being the empty
        textarea inside the block we already proved is the letter's.
    "Attach"/"Upload" is never clicked: a file chooser opens an OS dialog, and at 3am
    there is nobody to dismiss it.
    """
    field = form.locator('textarea[id*="cover_letter" i], textarea[name*="cover" i]')
    if not await field.count():
        # ONLY the "enter manually" trigger. Greenhouse renders the letter block as four
        # buttons — Attach, Dropbox, Google Drive, Enter manually — and the first three
        # hand control to somebody else: a file chooser is an OS dialog, Dropbox and
        # Drive are OAuth popups. At 3am there is nobody to dismiss any of them. Live
        # 09-25 on this very form, a filter that only excluded "attach|upload" clicked
        # Dropbox, counted the block as opened, and no textarea ever came back. The
        # extension has always matched the testid suffix instead; so do we.
        trigger = form.locator(
            '[data-testid$="-text" i][data-testid*="cover" i], [data-testid*="cover_letter_text" i]'
        )
        if not await trigger.count():
            trigger = form.get_by_role(
                "button", name=re.compile(r"^\s*(enter|type)\s+manually|paste|write", re.I)
            )
        opened = False
        for k in range(min(await trigger.count(), 6)):
            btn = trigger.nth(k)
            with contextlib.suppress(Exception):
                if not await btn.is_visible():
                    continue
                # Prove this trigger belongs to the COVER LETTER block, not the résumé's
                # identical chooser — walk up until an ancestor actually names the field.
                named = await btn.evaluate(
                    "e => { const t=(e.getAttribute('data-testid')||'').toLowerCase();"
                    " if (t.includes('cover')) return true;"
                    " let n=e; for (let h=0; h<6 && n; h++) {"
                    "   if (/cover\\s*letter/i.test(n.textContent||'')) return true; n=n.parentElement; }"
                    " return false; }"
                )
                if not named:
                    continue
                await btn.click(timeout=8000)
                await page.wait_for_timeout(1500)
                opened = True
                break
        if not opened:
            log("  ✉ no cover-letter field on this form — none written (honest zero)")
            return ""
        # Greenhouse hydrates the revealed textarea late; give it room before deciding.
        for _ in range(6):
            field = form.locator('textarea[id*="cover_letter" i], textarea[name*="cover" i]')
            if await field.count():
                break
            await page.wait_for_timeout(1000)
        if not await field.count():
            log("  ✉ clicked the trigger but no textarea appeared — letter NOT delivered")
            return ""

    letter = ""
    with contextlib.suppress(Exception):
        letter = generate_cover_letter(
            {
                "title": job.get("title"),
                "company": job.get("company"),
                "description": job.get("description") or "",
                "link": job.get("link"),
            },
            profile,
        )
    if not letter:
        log("  ✉ letter generation returned nothing — leaving the field empty")
        return ""
    await field.first.fill(letter)
    # Read it back: a textarea that silently refused the text would otherwise be recorded
    # in History as a letter the employer never saw.
    typed = ""
    with contextlib.suppress(Exception):
        typed = await field.first.input_value()
    if not typed.strip():
        log("  ✉ field would not take the text — letter NOT delivered")
        return ""
    log(f"  ✉ cover letter delivered ({len(typed)} chars)")
    return typed


# What a react-select actually HOLDS. The chosen value is rendered inside the control as a
# sibling of the input's own container, so the old `closest('div[class*=select]')` stopped
# at `select__input-container` and found nothing — on every Greenhouse dropdown, always.
# Measured on the live DoorDash form 09-30: "already answered" never fired and the
# read-back after each click returned "", so the log printed the answer we MEANT.
_HELD_JS = (
    "e => { const c = e.closest('[class*=\"select__control\"]')"
    " || e.closest('div[class*=select]');"
    " const v = c && c.querySelector('[class*=\"single-value\"], [class*=singleValue]');"
    " return v ? v.textContent.trim() : ''; }"
)


async def held_value(box) -> str:
    with contextlib.suppress(Exception):
        return (await box.evaluate(_HELD_JS)) or ""
    return ""


async def close_menu(page) -> None:
    """Escape — but ONLY while a menu is open. On a closed react-select the same key (and
    an emptied input) clears the value that was just chosen: live 09-30 a correctly
    picked "Honolulu, Hawaii, United States" was wiped by its own clean-up."""
    with contextlib.suppress(Exception):
        if await page.locator('[class*="select__menu"]').count():
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(150)


def _miss(missed: list[str], label: str, optional: bool) -> None:
    """An unanswered dropdown blocks the submit only where the form requires an answer.
    Greenhouse marks its optional ones (`aria-required="false"` — School and Degree on
    most forms); blank there is an honest application, not a hand-back."""
    if optional:
        log(f"  – {label[:60]}: left blank (optional, no answer on file)")
    else:
        missed.append(label)


async def choose(
    page,
    box,
    opts,
    index: int,
    option: str,
    options: list[str],
    label: str,
    note: str,
    missed: list[str],
    optional: bool,
) -> None:
    """Click the option at `index`, shut the menu, and log what the WIDGET now holds."""
    await opts.nth(index).click(timeout=8000)
    await page.wait_for_timeout(300)
    # Leave no menu open behind us — the next combobox has to be clickable.
    await close_menu(page)
    # Clicking is not choosing, and a log line that reports our intent instead of the
    # field's state is how a wrong answer reaches an employer looking correct in the
    # transcript.
    settled = await held_value(box)
    if settled and not same_value(option, settled, options):
        _miss(missed, label, optional)
        log(f"  ! {label[:50]}: wanted {option[:30]!r}, widget holds {settled[:30]!r}")
        return
    log(f"  ▾ {label[:60]} → {settled or option}{note}")


async def fill_typeahead(page, box, kind: str, profile: dict) -> str:
    """Type the profile's fact into a search-fed react-select and take the option that
    IS that fact. Returns what the widget holds afterwards, "" = left empty.
    """
    box_id = (await box.get_attribute("id")) or ""
    # Scoped by the input's id, like every other dropdown here. The caller's locator is
    # no use: it was resolved while the menu was still empty.
    opts = (
        page.locator(f'[id^="react-select-{box_id}-option"]')
        if box_id
        else page.locator('[class*="select__menu"] [class*="select__option"]')
    )
    for query in typeahead_queries(kind, profile):
        # Nothing is chosen yet at this point, so emptying the input is safe — after a
        # choice it would clear it (see close_menu).
        await box.fill("")
        await box.type(query, delay=35)
        # The options arrive from a search request; wait for rows, not for a timer.
        texts: list[str] = []
        for _ in range(12):
            await page.wait_for_timeout(500)
            texts = [
                re.sub(r"\s+", " ", (await opts.nth(j).inner_text()) or "").strip()
                for j in range(min(await opts.count(), 40))
            ]
            if any(texts):
                break
        target = pick_typeahead(kind, query, [t for t in texts if t], profile)
        if not target:
            continue
        await opts.nth(texts.index(target)).click(timeout=8000)
        await page.wait_for_timeout(300)
        # Same read-back rule as every other dropdown here: report the field, not intent.
        settled = await held_value(box)
        if same_value(target, settled, [t for t in texts if t]):
            await close_menu(page)
            return settled
    await close_menu(page)
    return ""


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
        label = ""
        optional = False
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
            # Only an explicit "false" is optional; a form that says nothing is treated
            # as requiring the answer, which errs toward the hand-back.
            optional = (await box.get_attribute("aria-required")) == "false"
            # Already answered (Greenhouse's own resume autofill, or an earlier pass)?
            if await held_value(box):
                continue

            # Close whatever menu is still hanging open before touching this one.
            # Live 09-25 (Amplitude): the disability dropdown's menu stayed open and
            # physically covered the next combobox — Playwright retried the click for
            # 30 seconds and gave up, and every question after it stayed blank. An open
            # menu is also why scoping matters below: two menus open at once means the
            # page-wide option locator reads someone else's answers.
            await close_menu(page)

            await box.click(timeout=8000)
            await page.wait_for_timeout(600)

            # Scope the options to THIS combobox's own menu. react-select ids its options
            # `react-select-<input id>-option-N`, so the input's id is the key. A
            # page-wide locator would happily hand us the options of a different
            # question — the same class of silent wrong-answer bug as the index shift.
            box_id = (await box.get_attribute("id")) or ""
            opts = (
                page.locator(f'[id^="react-select-{box_id}-option"]')
                if box_id
                else page.locator('[class*="select__option"], [role="option"]')
            )
            if box_id and not await opts.count():
                opts = page.locator('[class*="select__menu"] [class*="select__option"]')
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

            # DEMOGRAPHIC QUESTIONS ARE NOT OURS TO ANSWER. These are voluntary
            # self-identification (EEO/OFCCP): gender, race, orientation, disability,
            # veteran status. Live 09-25 the model read the résumé and declared
            # "Gender Identity → Male" — probably right, and still a statement about a
            # person made by a machine that was never told it. Every such form offers a
            # decline option; taking it is the one answer that claims nothing, cannot be
            # held against the candidate, and remains the user's to change. It also
            # skips an AI call.
            if texts and _DEMOGRAPHIC_Q.search(label):
                decline = next((t for t in texts if _DECLINE_OPT.search(t)), None)
                if decline:
                    await choose(
                        page,
                        box,
                        opts,
                        pairs[texts.index(decline)][0],
                        decline,
                        texts,
                        label,
                        " (declined on purpose)",
                        missed,
                        optional,
                    )
                    continue

            # AN OPT-IN IS NOT A QUESTION ABOUT THE CANDIDATE. "May we text you?" is the
            # board asking for a channel, and nothing about the application depends on
            # it — the extension has always answered No (content.js, the SMS rule). Here
            # the model answered instead: live 09-30 it opted Igor into SMS and WhatsApp
            # messages from an employer, a consent he was never asked for.
            if texts and _OPT_IN_Q.search(label):
                no = next((t for t in texts if _NO_OPT.match(t)), None)
                if no:
                    await choose(
                        page,
                        box,
                        opts,
                        pairs[texts.index(no)][0],
                        no,
                        texts,
                        label,
                        " (opt-in declined)",
                        missed,
                        optional,
                    )
                    continue

            # A SEARCH BOX, NOT A LIST: the fact comes from the profile and is typed in.
            # School always (its menu opens on an arbitrary first page of thousands);
            # location only when the menu opened empty — a fixed list of offices is an
            # ordinary question for the answerer below.
            kind = typeahead_kind(label)
            if kind == "school" or (kind == "location" and not texts):
                settled = await fill_typeahead(page, box, kind, profile)
                if settled:
                    log(f"  ⌕ {label[:60]} → {settled}")
                else:
                    _miss(missed, label, optional)
                continue

            if not texts:
                _miss(missed, label, optional)
                await close_menu(page)
                continue
            answer = night_answer(
                label,
                {
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "description": job.get("description") or "",
                },
                profile,
                texts,
            )
            if not answer:
                _miss(missed, label, optional)
                await close_menu(page)
                continue
            # The answerer returns one option verbatim; fall back to the closest match
            # rather than typing free text into a closed list.
            target = (
                answer
                if answer in texts
                else next((t for t in texts if answer.lower() in t.lower()), None)
            )
            if not target:
                _miss(missed, label, optional)
                await close_menu(page)
                continue
            await choose(
                page,
                box,
                opts,
                pairs[texts.index(target)][0],
                target,
                texts,
                label,
                "",
                missed,
                optional,
            )
        except Exception as exc:  # noqa: BLE001
            # A combobox that blew up is UNANSWERED, and the pre-submit gate has to hear
            # about it: the Amplitude run swallowed nine failures and still offered the
            # form as fillable. Also clear the menu, or this failure cascades into every
            # question below it.
            short = str(exc).split("\n")[0][:90]
            log(f"  ! combobox #{i} failed: {short}")
            await close_menu(page)
            if label:
                _miss(missed, label, optional)
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


# Page-loads a walk may spend per application it is allowed to send.
WALK_PER_SUBMIT = 40


def _note(user_id: str, level: str, message: str, metadata: dict | None = None) -> None:
    """One night-shift line in the user's activity log. Never raises: the application
    either went or it did not, and a logging failure must not change which."""
    with contextlib.suppress(Exception):
        get_supabase().table("activity_log").insert(
            {
                "user_id": user_id,
                "level": level,
                "phase": "night-shift",
                "message": message,
                "metadata_json": metadata or {},
            }
        ).execute()


async def apply_one(
    page, form, job: dict, profile: dict, user_id: str, resume_path: str, live: bool
) -> str:
    """Fill ONE live form and — in live mode — send it. Returns the outcome word.

    Live: `sent`, `knockout`, `handback`, `no_submit`, `capped`, or the board's refusal
    (`invalid` | `captcha_code` | `flagged` | `unknown`). Dry-run never clicks Submit and
    answers what a live run WOULD have done up to that click: `dry` (ready to send),
    `knockout` or `handback`.
    """
    platform = job.get("platform") or "greenhouse"
    if platform == "ashby":
        unfilled, letter, knocked = await ashby.fill(page, form, profile, job, resume_path)
    else:
        unfilled, letter = await fill_greenhouse(page, form, profile, job, resume_path)
        knocked = await knockout_answers(form)

    # KNOCKOUT: the form asked a hard requirement and our honest answer was "no".
    # Sending anyway spends a daily slot on a guaranteed rejection and tells the
    # employer we did not read their posting. Live 09-26 (Igor): an SF hybrid role
    # asked "are you currently located in the Bay Area and able to work from our
    # office?", the honest answer was No — and the run was about to submit.
    if knocked:
        log(f"KNOCKOUT — the form's own requirement rules this candidate out: {knocked[0]}")
        if live:
            _note(
                user_id,
                "info",
                f"🌙 Night shift stood down [outcome=knockout]: {job['title']}"
                f" @ {job.get('company', '?')} — {knocked[0]}",
            )
            return "knockout"

    shot = os.path.join(SHOTS, f"{job['id']}-filled.png")
    await page.screenshot(path=shot, full_page=True)
    log(f"screenshot: {shot}")

    if unfilled:
        log(f"required-but-empty: {unfilled}")
        if live and REQUIRED_EMPTY_IS_FATAL:
            record_handback(user_id, job, platform, unfilled)
            log(
                "LIVE ABORTED — a required field has no honest answer (handed back to"
                " the user, not guessed)."
            )
            return "handback"

    if not live:
        log("dry-run complete — nothing submitted.")
        return "knockout" if knocked else "handback" if unfilled else "dry"

    submit = (
        form.locator(ashby.SUBMIT)
        if platform == "ashby"
        else form.locator('button[type="submit"], button:has-text("Submit application")')
    )
    if not await submit.count():
        log("LIVE ABORTED — no submit button found")
        return "no_submit"

    # THE CAP IS THE BACKEND'S, AND THIS PATH HAS TO ASK IT TOO.
    # Until now the night shift wrote `applications` rows straight through the
    # service_role client, so the only real gate in the product — check_can_apply
    # behind POST /applications/save, which counts today's rows and 429s the 31st —
    # never saw a server-side submit. One writer obeyed the 30/day, 15/platform,
    # free-taste and expired-subscription limits; the other did not know they existed.
    # Asked HERE, immediately before the click: the answer must be as fresh as
    # possible, since the extension may have been applying on the user's machine
    # while this walk was reading forms.
    gate = check_can_apply(user_id, platform, profile.get("email"))
    if not gate.get("allowed"):
        log(f"LIVE ABORTED — cap reached: {gate.get('reason')}")
        _note(
            user_id,
            "info",
            f"🌙 Night shift stood down [outcome=capped]: {gate.get('reason')}"
            f" ({gate.get('used_today')}/{gate.get('daily_limit')} today).",
        )
        return "capped"
    watch = SubmitWatch()
    watch.attach(page)  # armed BEFORE the click — the verdict rides the submit XHR
    await submit.first.click()
    # FROM HERE THE APPLICATION MAY ALREADY BE WITH THE EMPLOYER. Anything that goes wrong
    # while reading the result must not leave the job looking untouched: the next walk
    # would open it and send a second one. So a crash past this line is a hand-back too.
    try:
        judge = ashby.submit_outcome if platform == "ashby" else submit_outcome
        confirmed, why, outcome = await judge(page, form, watch)
        with contextlib.suppress(Exception):
            await page.screenshot(
                path=os.path.join(SHOTS, f"{job['id']}-after-submit.png"), full_page=True
            )
    except Exception as exc:  # noqa: BLE001
        confirmed, outcome = False, "unknown"
        why = f"Submit was clicked, but the result could not be read ({str(exc).splitlines()[0][:80]})"
    log(f"submit outcome: {outcome.upper()} — {why} | http {watch.status} | {page.url}")

    if not confirmed:
        # NOT SENT is not a weaker kind of applied — it is a hand-back. Live 09-23
        # recorded a Zocdoc application the employer never received (three required
        # comboboxes were empty, the page simply re-rendered with validation errors)
        # because the old detector matched the word "submitted" in the button label.
        # A row written without proof of sending makes silent failure look like work.
        #
        # The outcome word is machine-countable on purpose: `outcome=captcha_code`
        # per hundred submits IS the reputation metric (P2), and `outcome=invalid`
        # separates our own filling bugs from Greenhouse's judgement of us.
        outcome = outcome if outcome != "sent" else "unknown"
        record_handback(
            user_id,
            job,
            platform,
            [],
            reason=(
                f"We could not confirm this one was sent [http={watch.status}]: {why}."
                " Nothing is recorded as applied — check your inbox, then finish it by hand"
            ),
            outcome=outcome,
        )
        return outcome

    status = "applied"
    try:
        jobs_db.mark_applied_by_link(user_id, job["link"], status)
        apps_db.save_application(
            user_id=user_id,
            job_id=job["id"],
            cover_letter=letter,
            status=status,
            job_title=job["title"],
            company=job.get("company") or "",
            platform=platform,
            job_url=job["link"],
        )
    except Exception as exc:  # noqa: BLE001
        # SENT AND UNRECORDED is the one state that produces a duplicate: the dedup reads
        # the applications table, and this posting is not in it. The employer has the
        # application; the open hand-back is what keeps tomorrow's walk away from it.
        log(f"  ! SENT but NOT recorded ({str(exc).splitlines()[0][:120]})")
        record_handback(
            user_id,
            job,
            platform,
            [],
            reason=(
                "This application WAS sent, but we could not save it to your history."
                " Do not apply to it again"
            ),
            outcome="sent_unrecorded",
        )
        return "sent"
    # The lifetime free-taste counter lives beside the daily cap and is advanced by
    # POST /applications/save, which this path bypasses. Without this a free user's
    # night submits would never count toward FREE_APP_LIMIT — the gate would stay
    # open forever for exactly the applications nobody is watching.
    if gate.get("tier") == "free":
        with contextlib.suppress(Exception):
            increment_free_apps(user_id)
    _note(
        user_id,
        "info",
        f"🌙 Night shift applied (server) [outcome=sent http={watch.status} board={platform}]:"
        f" {job['title']} @ {job.get('company', '?')} [{status}]",
    )
    log("recorded: applications + jobs status + activity line")
    return "sent"


async def run(
    user_id: str,
    job_url: str | None,
    live: bool,
    headful: bool,
    platforms: tuple = PLATFORMS,
    max_jobs: int = 1,
) -> list[str]:
    """Walk the candidates and apply to up to `max_jobs` of them. Returns one outcome
    word per form that was actually filled (see apply_one)."""
    profile = get_profile(user_id)
    # Email lives in Supabase auth, not profiles (same gap content.js patches from the JWT).
    if not profile.get("email"):
        admin = get_supabase().auth.admin.get_user_by_id(user_id)
        profile["email"] = admin.user.email if admin and admin.user else ""
    # WORK SETTING IS REQUIRED FOR AN UNWATCHED SUBMIT. Empty means "never asked", not
    # "anything goes": the first live pick (09-26) was an SF hybrid role for a Honolulu
    # user whose setting was blank. The dashboard makes the pick before Start; the server
    # refuses to guess it for a run nobody is watching. Dry-run only warns.
    setting = (profile.get("work_setting") or "").strip().lower()
    if not setting:
        if live:
            raise SystemExit(
                "LIVE REFUSED — work setting is not chosen (Remote / Hybrid / On-site)."
                " Pick it on the dashboard, then run again."
            )
        log("! work setting not chosen — dry-run continues as 'any'; --live would refuse")
    max_jobs = max(1, max_jobs)
    candidates = pick_jobs(user_id, job_url, profile, platforms)
    # What a stored verdict was judged against (resume, mode, search) — a row carrying
    # another version goes back to the judge instead of being trusted.
    version = verdict_version(profile, resume_text_for(profile))
    user_loc = parse_user_location(profile.get("location") or "")
    resume_path = download_resume(user_id, profile)
    log(f"resume: {resume_path} | candidates: {len(candidates)} | to send: up to {max_jobs}")

    outcomes: list[str] = []
    capped: set[str] = set()
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

        # Bounded walk: a night may send `max_jobs`, and may not open the whole pool
        # looking for them.
        for cand in candidates[: WALK_PER_SUBMIT * max_jobs]:
            platform = cand.get("platform") or "greenhouse"
            is_ashby = platform == "ashby"
            if platform in capped:
                continue
            # US ONLY, AND THE TITLE CAN SAY OTHERWISE. "Influencer Marketing Coordinator
            # (Canada)" sits in the pool with the location "Remote", which the country
            # gate reads as fine. Dry-run 09-30: the form asked "Are you legally
            # authorized to work in Canada?" and the answer on its way out was Yes.
            if not job_url and names_foreign_country(cand.get("title") or ""):
                log(f"skip (title names another country): {cand['title']}")
                continue
            url = ashby.application_url(cand["link"]) if is_ashby else cand["link"]
            log(
                f"try: [{cand.get('platform')}] {cand['title']} @ {cand.get('company', '?')}"
                f" — {cand.get('location') or 'no location'}\n     {url}"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001 — a dead host is a skip, not a crash
                log(f"  skip (nav failed: {exc})")
                continue
            await page.wait_for_timeout(3000)
            form = await (ashby.find_form(page) if is_ashby else find_application_form(page))
            if form is None:
                # GH answers a closed posting with a redirect to the board root
                # (`?error=true`) — bury the row so no run re-opens this corpse. Live
                # 09-23: the twelve freshest candidates were ALL dead; without burial
                # every future walk pays the same twelve page-loads first. Only in
                # auto-pick mode: an explicit --job-url misfire must not retire a row.
                # Ashby has no equivalent redirect we have seen live — skip, never retire.
                if (
                    not is_ashby
                    and not job_url
                    and ("error=true" in page.url or "/jobs/" not in page.url)
                ):
                    n = jobs_db.mark_dead_link(user_id, cand["link"])
                    log(f"  skip (dead posting — retired {n} pool row(s))")
                else:
                    log(f"  skip (no application form on {page.url})")
                continue
            # The same fit bar the extension applies — the SERVER side of one engine.
            # A live submit that skips the judge would send the LCSW postings this
            # user's résumé can never pass (dry-run #4 landed on exactly one).
            # CHEAP, CERTAIN CHECKS BEFORE THE PAID ONE. Location and "did we already
            # send this" are free and deterministic; the fit judge is a model call. They
            # used to run in the opposite order, so every job we were going to reject on
            # geography was still judged first, at full price.

            # THE POOL IS AN ARCHIVE, NOT A QUEUE. Its rows were harvested without the
            # user's location in the sweep, so the filter must be applied again HERE, at
            # submit time — the dashboard already does exactly this when it shows the
            # deck. Live 09-26, Igor caught it: an on-site/hybrid San Francisco role was
            # picked for a candidate in Honolulu with a 10-mile radius, and the fit judge
            # still scored it 72. That application spends a daily slot on a certain
            # rejection and tells the employer we did not read their posting.
            where = location_verdict(cand.get("location") or "", user_loc)
            if where == "elsewhere":
                log(
                    f"  skip (location: {cand.get('location')!r}"
                    f" is outside {profile.get('location')})"
                )
                continue

            # Arrangement, separately from place: an office role in the user's own city
            # passes the check above, and is still wrong for someone who asked for remote.
            if not matches_work_setting(
                cand.get("location") or "", cand.get("title") or "", setting
            ):
                log(f"  skip (work setting: {cand.get('location')!r} is not remote)")
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

            # The verdict the queue already holds is THE verdict: it is the score the
            # user sees next to this job, and build_queue has dropped everything below
            # their bar. Only a row nobody could judge yet is judged here, at the form.
            if has_current_verdict(cand, version):
                # Queue rows are above the bar by construction; an explicit --job-url
                # pick did not come through the queue, so the bar is checked here too.
                if (cand.get("fit_score") or 0) < mode_threshold(profile):
                    log(f"  skip (fit {cand.get('fit_score')}, prejudged — below the bar)")
                    continue
                log(f"  fit {cand.get('fit_score')} (prejudged) — proceeding")
            else:
                fit = assess_fit(job=cand, profile=profile)
                if fit.get("decision") != "apply":
                    log(f"  skip (fit {fit.get('fit_score')}): {str(fit.get('reason') or '')[:80]}")
                    continue
                log(f"  fit {fit.get('fit_score')} — proceeding")

            try:
                outcome = await apply_one(page, form, cand, profile, user_id, resume_path, live)
            except Exception as exc:  # noqa: BLE001 — one broken form must not end the night
                outcome = "error"
                log(
                    f"  ! this form broke the filler ({str(exc).splitlines()[0][:120]}) — moving on"
                )
            outcomes.append(outcome)
            log(f"outcome={outcome} [{len(outcomes)}] {cand['title']} @ {cand.get('company', '?')}")
            if outcome == "capped":
                capped.add(platform)
            stop = walk_verdict(outcomes, max_jobs, capped, platforms)
            if stop:
                log(f"walk stops: {stop}")
                break
            # A fresh page per form: no half-filled inputs, listeners or leave-page
            # prompts carried from one employer's application into the next.
            with contextlib.suppress(Exception):
                await page.close()
            page = await ctx.new_page()

        if not outcomes:
            log("no candidate with a live application form in this batch — nothing to do")
        else:
            tally = ", ".join(f"{k}={n}" for k, n in Counter(outcomes).most_common())
            log(f"walk finished: {len(outcomes)} form(s) — {tally}")
        await browser.close()
    return outcomes


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--job-url")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--platform", choices=[*PLATFORMS, "all"], default="all")
    ap.add_argument(
        "--max",
        type=int,
        default=1,
        help="applications this run may send (dry-run: forms it may fill). Default 1.",
    )
    args = ap.parse_args()
    boards = PLATFORMS if args.platform == "all" else (args.platform,)
    asyncio.run(run(args.user, args.job_url, args.live, args.headful, boards, args.max))
