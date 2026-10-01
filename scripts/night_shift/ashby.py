"""Night-shift adapter for Ashby (jobs.ashbyhq.com/<org>/<uuid>/application).

Same contract as the Greenhouse path in executor.py: find the form → fill it (scope =
the form only) → list what stayed empty → name any knockout → submit → POSITIVE proof.

Markup read from a live form (Vanta, 09-27), not guessed (memory: fixtures-must-be-
captured). Every question is a `[data-field-path]` entry whose title label carries a
`_required_…` class when the answer is mandatory — Ashby does NOT put `required` on most
controls (only on the system text fields), so the label class is the only honest
signal. Widgets seen:
  * text / email / tel / textarea — `.ashby-application-form-input-text`, textarea
  * yes/no — two buttons `.ashby-application-form-input-yesno-option[data-option]`
    with `aria-pressed`, the real value in a hidden checkbox
  * autocomplete (Location) — `input[role=combobox]`, options render as `[role=option]`
  * radio group (EEO and short single-selects) — `.ashby-application-form-input-radio-group-option`
  * file — `_systemfield_resume` (+ a separate "Autofill from resume" uploader we skip)
The SMS-consent radios under the phone field are never touched: agreeing to text
messages is a consent only the person can give.
"""

import contextlib
import re

from common import _DECLINE_OPT, _DEMOGRAPHIC_Q, is_knockout, log
from common import answer as night_answer

from modules.ai_cover_letter import generate_cover_letter

ENTRY = "[data-field-path]"
SUBMIT = "button.ashby-application-form-submit-button, button:has-text('Submit Application')"

# A posting URL from the pool sometimes carries a doubled path (`/application/application`)
# that renders a form-less stub (ext background.js, 08-04); the base posting is live.
_DOUBLED = re.compile(r"(/application)+/?$")


def application_url(link: str) -> str:
    base = _DOUBLED.sub("", (link or "").split("?")[0].rstrip("/"))
    return base + "/application"


async def find_form(page):
    """The application form's root, or None. Ashby hydrates late — wait for an entry.

    There is no <form> element (live 09-27), and the EEO survey sits in a SEPARATE
    `.ashby-application-form-container` from the questions, so neither selector covers
    the whole application. The root is therefore the nearest ancestor holding every
    entry AND the submit button — marked once, then addressed like any other locator.
    """
    with contextlib.suppress(Exception):
        await page.wait_for_selector(ENTRY, timeout=15000)
    marked = await page.evaluate(
        """() => {
          const parts = [...document.querySelectorAll('[data-field-path]')];
          const btn = document.querySelector('.ashby-application-form-submit-button');
          if (!parts.length || !btn) return false;
          let root = btn.parentElement;
          while (root && !parts.every(p => root.contains(p))) root = root.parentElement;
          if (!root) return false;
          root.setAttribute('data-hd-form', '1');
          return true;
        }"""
    )
    return page.locator("[data-hd-form]").first if marked else None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").replace("*", "").strip()


def _job_ctx(job: dict) -> dict:
    return {
        "title": job.get("title"),
        "company": job.get("company"),
        "description": job.get("description") or "",
    }


async def fill(page, form, profile: dict, job: dict, resume_path: str):
    """Returns (unfilled_required, letter_text, knockouts)."""
    full = " ".join(
        p
        for p in ((profile.get("name") or "").strip(), (profile.get("last_name") or "").strip())
        if p
    )
    system = {
        "_systemfield_name": full,
        "_systemfield_email": (profile.get("email") or "").strip(),
    }

    # Résumé first: Ashby parses the upload and may prefill fields, and everything we
    # type afterwards must win over its guesses.
    resume = form.locator('[data-field-path="_systemfield_resume"] input[type=file]')
    if await resume.count():
        await resume.first.set_input_files(resume_path)
        await page.wait_for_timeout(4000)

    unfilled: list[str] = []
    knocked: list[str] = []
    letter = ""
    entries = form.locator(ENTRY)
    for i in range(await entries.count()):
        entry = entries.nth(i)
        label = ""
        try:
            path = (await entry.get_attribute("data-field-path")) or ""
            title = entry.locator(".ashby-application-form-question-title").first
            if not await title.count():
                continue
            label = _clean(await title.inner_text())
            required = "_required" in ((await title.get_attribute("class")) or "")
            if path == "_systemfield_resume":
                continue

            # ── yes / no ──────────────────────────────────────────────────────────
            yesno = entry.locator(".ashby-application-form-input-yesno-option")
            if await yesno.count():
                if await entry.locator('[aria-pressed="true"]').count():
                    continue
                answer = night_answer(label, _job_ctx(job), profile, ["Yes", "No"])
                pick = (answer or "").strip().lower()
                if pick not in ("yes", "no"):
                    if required:
                        unfilled.append(label)
                    continue
                await entry.locator(f'[data-option="{pick}"]').first.click()
                # Read back from the widget, not from our intent (audit 09-26: a dropdown
                # clicked the wrong option while the log printed the right one).
                pressed = entry.locator(f'[data-option="{pick}"][aria-pressed="true"]')
                if not await pressed.count():
                    log(f"  ! {label[:60]}: clicked {pick}, widget did not take it")
                    unfilled.append(label)
                    continue
                log(f"  · {label[:60]} → {pick.title()}")
                if is_knockout(label, pick):
                    knocked.append(f"{label[:90]} → {pick.title()}")
                continue

            # ── radio group (EEO, short single-selects) ────────────────────────────
            radios = entry.locator(".ashby-application-form-input-radio-group-option")
            if await radios.count():
                if await entry.locator("input[type=radio]:checked").count():
                    continue
                opts = [_clean(t) for t in await radios.all_inner_texts()]
                if _DEMOGRAPHIC_Q.search(label):
                    # Declined on principle, never inferred from a CV (same as GH).
                    choice = next((o for o in opts if _DECLINE_OPT.search(o)), None)
                else:
                    choice = night_answer(label, _job_ctx(job), profile, opts)
                if not choice or choice not in opts:
                    if required:
                        unfilled.append(label)
                    continue
                chosen = radios.nth(opts.index(choice))
                await chosen.locator("label").first.click()
                if not await chosen.locator("input[type=radio]:checked").count():
                    log(f"  ! {label[:60]}: clicked {choice[:40]!r}, radio not checked")
                    unfilled.append(label)
                    continue
                log(f"  · {label[:60]} → {choice[:60]}")
                if is_knockout(label, choice):
                    knocked.append(f"{label[:90]} → {choice[:30]}")
                continue

            # ── autocomplete (Location and long single-selects) ────────────────────
            combo = entry.locator('input[role="combobox"]')
            if await combo.count():
                if (await combo.first.input_value()).strip():
                    continue
                if path == "_systemfield_location":
                    query = (profile.get("city") or "").strip() or (
                        (profile.get("location") or "").split(",")[0].strip()
                    )
                else:
                    query = night_answer(label, _job_ctx(job), profile)
                picked = await _pick_option(page, combo.first, query)
                if picked:
                    log(f"  · {label[:60]} → {picked[:60]}")
                elif required:
                    unfilled.append(label)
                continue

            # ── other files (cover letter upload, portfolio) — no OS dialogs at 3am ──
            if await entry.locator("input[type=file]").count():
                if required:
                    unfilled.append(label)
                continue

            # ── checkbox groups / multi-selects: a wrong tick is a statement in the
            # user's name, so they are handed back, never guessed.
            if (
                await entry.locator("input[type=checkbox]").count()
                and not await entry.locator(
                    "input[type=text], input[type=email], input[type=tel], textarea"
                ).count()
            ):
                if required:
                    unfilled.append(label)
                continue

            # ── text / email / tel / textarea ──────────────────────────────────────
            box = entry.locator(
                "input.ashby-application-form-input-text, input[type=text], input[type=email],"
                " input[type=tel], input[type=url], input[type=number], textarea"
            ).first
            if not await box.count():
                if required:
                    unfilled.append(label)
                continue
            value = system.get(path, "")
            if path in system and not value:
                # Name/email come from the profile or nowhere — never from a model.
                if required:
                    unfilled.append(label)
                continue
            typ = ((await box.get_attribute("type")) or "").lower()
            if not value and typ == "tel":
                value = (profile.get("phone") or "").strip()
            if not value and re.search(r"linkedin", label, re.I):
                value = (profile.get("linkedin_url") or "").strip()
            if not value and re.search(r"cover letter", label, re.I):
                # Lazy, like GH: the letter is written only when the form asks for it.
                with contextlib.suppress(Exception):
                    value = generate_cover_letter(
                        {**_job_ctx(job), "link": job.get("link")}, profile
                    )
                if value:
                    letter = value
            if not value and _DEMOGRAPHIC_Q.search(label):
                # "Pronouns (optional)" and friends: never inferred.
                if required:
                    unfilled.append(label)
                continue
            if not value:
                if (await box.input_value()).strip():
                    continue  # résumé autofill already answered it
                value = night_answer(label, _job_ctx(job), profile, numeric=typ == "number")
                if value and " " not in value.strip():
                    value = value.strip().rstrip(".")
            if not value:
                if required:
                    unfilled.append(label)
                continue
            await box.fill(value)
            if letter and value == letter:
                # Read it back — History must only claim a letter the employer can see.
                typed = await box.input_value()
                letter = typed if typed.strip() else ""
                log(
                    f"  ✉ cover letter {'delivered' if letter else 'NOT delivered'} ({len(letter)} chars)"
                )
            else:
                log(f"  · {label[:60]} → {value[:60]}")
        except Exception as exc:  # noqa: BLE001 — one odd widget must not kill the walk
            log(f"  ! {label[:50] or f'entry #{i}'} failed: {exc}")
            if label:
                unfilled.append(label)
    return unfilled, letter, knocked


async def _pick_option(page, box, query: str) -> str:
    """Type into an Ashby autocomplete and click the first suggestion. Returns the text
    now in the box, or "" — never leaves free text that is not one of the options."""
    if not query:
        return ""
    await box.click()
    await box.fill("")
    await box.type(query[:40], delay=40)
    opt = page.locator('[role="option"]')
    for _ in range(10):
        if await opt.count():
            break
        await page.wait_for_timeout(500)
    if not await opt.count():
        await box.fill("")
        return ""
    await opt.first.click()
    await page.wait_for_timeout(500)
    return (await box.input_value()).strip()


_SENT_TEXT = re.compile(
    r"thank you for applying|application (?:was |has been )?(?:successfully )?submitted"
    r"|we['’]ve received your application|application received",
    re.I,
)
_FLAGGED_TEXT = re.compile(r"spam|recaptcha|suspicious|could ?n['’]?o?t submit", re.I)


async def submit_outcome(page, form, watch=None) -> tuple[bool, str, str]:
    """(sent, why, outcome) — POSITIVE proof only, validation first (see executor).

    Ashby submits over GraphQL and answers 200 even when it refuses, so the HTTP code
    alone proves nothing here; the page is the verdict. outcome: sent | invalid |
    flagged (Ashby's own spam/captcha refusal — the reputation number for this board) |
    unknown.
    """
    success = page.locator(".ashby-application-form-success-container")
    for _ in range(25):
        with contextlib.suppress(Exception):
            if await success.count():
                return True, "success container", "sent"
            body = await page.inner_text("body")
            if _SENT_TEXT.search(body):
                return True, "confirmation text", "sent"
        await page.wait_for_timeout(1000)

    errors = page.locator(
        '[class*="_error"]:visible, [role="alert"]:visible, text=/needs corrections|is required/i'
    )
    n_err = 0
    with contextlib.suppress(Exception):
        n_err = await errors.count()
    body = ""
    with contextlib.suppress(Exception):
        body = await page.inner_text("body")
    if _FLAGGED_TEXT.search(body):
        return False, "Ashby refused the submission as suspected spam/captcha", "flagged"
    if n_err:
        first = ""
        with contextlib.suppress(Exception):
            first = _clean(await errors.first.inner_text())[:60]
        return False, f"form refused it: {n_err} validation error(s) — {first}", "invalid"
    status = watch.status if watch else "?"
    return False, f"no proof of sending (form still on screen, http {status})", "unknown"
