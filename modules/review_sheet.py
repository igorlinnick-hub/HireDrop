"""The one look at everything we tell employers — before the FIRST run, once (Igor, 10-07).

"Когда человек делает первый прогон, всплывает окно со всей информацией, он проверяет, всё
ли ок, и подтверждает. Один раз, не постоянно." Signup asks the questions one screen at a
time; nobody ever saw them side by side with the contact details and the answers we pick
on our own. This sheet is that page: every value a form gets from the profile, editable,
and one "Everything's correct" that is recorded (`profiles.answers_confirmed_at`).

Owed only by an account that has never run (app/db/campaign.review_due) and never
confirmed. Accounts that already ran are not stopped to re-confirm — it is a once-only
check, not a lock, so a failed read never owes it either.

The list is the server's, like the employer questions it includes (modules/
employer_answers.py): the dashboard draws whatever rows it is handed, so the sheet, its
save and the Start gate cannot disagree about what is on it.
"""

from __future__ import annotations

from modules.employer_answers import OPT_OUT, form

# A dashboard that can draw this sheet says so with `answers_ui` (see employer_answers.SINCE).
# An older tab cannot, so the check is not owed to it — it would hold Start shut behind a
# row the tab has no form for.
REVIEW_SINCE = 3

# Contact rows the forms fill on every application. Email is the account's sign-in
# address (the extension reads it from the session), so it is shown, not edited here.
CONTACT: tuple[tuple[str, str], ...] = (
    ("name", "First name"),
    ("last_name", "Last name"),
    ("phone", "Phone"),
)
# What the filler puts in when the profile is blank (chrome-extension/content.js, the
# notice-period and English-level text fields). Shown pre-filled with exactly that, so the
# person sees the answer they would otherwise send without knowing.
DEFAULTED: tuple[tuple[str, str, str], ...] = (
    ("notice_period", "Notice period — how soon you could start", "2 weeks"),
    ("english_level", "English level", "Fluent"),
)
# Filled when present, left blank when not — no form is lost to a missing ZIP alone.
OPTIONAL: tuple[tuple[str, str], ...] = (("postal_code", "ZIP code"),)

# What POST /profile/review may write besides the employer answers (employer_answers.clean).
WRITABLE = (
    tuple(k for k, _ in CONTACT) + tuple(k for k, _, _ in DEFAULTED) + tuple(k for k, _ in OPTIONAL)
)
_MAX_TEXT = 200

EEO_LABEL = "Gender, race, veteran status, disability"
EEO_ANSWER = "We choose “I don't wish to answer” on every form"


def confirmed(profile: dict) -> bool:
    return bool(profile.get("answers_confirmed_at"))


def _text(key: str, label: str, value, *, required: bool = True) -> dict:
    return {
        "key": key,
        "label": label,
        "kind": "text",
        "value": str(value or "").strip(),
        "required": required,
    }


def sheet(profile: dict, email: str | None) -> list[dict]:
    """The sections, in order. Employer-answer rows come from employer_answers.form — the
    same rows, opt-outs and notes signup draws; the rest are drawn from the profile."""
    answers = {row["key"]: {**row, "required": True} for row in form(profile)}

    def take(*keys: str) -> list[dict]:
        return [answers[k] for k in keys if k in answers]

    defaulted = [
        _text(key, label, str(profile.get(key) or "").strip() or default)
        for key, label, default in DEFAULTED
    ]
    return [
        {
            "title": "Contact",
            "rows": [_text(k, label, profile.get(k)) for k, label in CONTACT]
            + [{"key": "email", "label": "Email", "kind": "info", "value": email or ""}],
        },
        {
            "title": "Where you live and can work",
            "rows": take("country", "city", "state")
            + [_text(k, label, profile.get(k), required=False) for k, label in OPTIONAL]
            + take("work_authorized_us", "needs_sponsorship"),
        },
        {
            "title": "Experience and education",
            "rows": take("current_title", "current_employer", "linkedin_url", "school", "degree"),
        },
        {
            "title": "Other questions employers ask",
            "rows": take("salary_expectation") + defaulted,
        },
        {
            "title": "Answered the same way on every form",
            "rows": [{"key": "eeo", "label": EEO_LABEL, "kind": "info", "value": EEO_ANSWER}],
        },
    ]


def flags(profile: dict) -> dict[str, bool]:
    """The "I don't have one" flags on file, for the opt-out boxes the rows carry."""
    return {flag: bool(profile.get(flag)) for flag in OPT_OUT}


def clean(body: dict) -> dict:
    """Keep only this sheet's own text keys, trimmed and bounded. Absent keys stay absent."""
    out: dict = {}
    for key in WRITABLE:
        if key in body:
            out[key] = str(body[key] or "").strip()[:_MAX_TEXT]
    return out


def incomplete(profile: dict) -> list[str]:
    """Required rows of this sheet's own that are still blank (the employer questions are
    checked by employer_answers.missing). The defaulted rows count as filled: blank, the
    filler sends their default, which is exactly what the sheet showed."""
    return [k for k, _ in CONTACT if not str(profile.get(k) or "").strip()]
