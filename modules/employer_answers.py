"""The questions employers almost always ask — answered once, at signup.

Measured 2026-09-27 over 30 days of hand-backs (`activity_db.handback_stats`): the forms
that stopped a run stopped on the same handful of questions — country 10, sponsorship 8,
city 7, work authorization 7, recent job title 5, recent employer 5, LinkedIn 4. Almost
all of them already had a profile field; people simply never filled it, so the filler
reached 99% of a form and handed it back. Igor, 09-27: block Start until every one is
answered — "один раз ответил и всё готово".

This list is the ONE authority: onboarding asks all of it (`GET /profile/employer-answers`),
`/campaign/readiness` shows what is still missing from it, `/campaign/start` refuses on it,
and every screen renders its form from what the server returns — so they can never
disagree about which questions count. The Start gate stays as the backstop for people who
signed up before a question existed.

School, degree and salary expectation joined on 2026-09-30, and not from the hand-back
measurement: the first server-side walk stopped on "School" and met "What are your salary
expectations?" on 4 of 8 forms. The night shift did not record its stops as hand-backs,
so neither could have shown up in the numbers above (the night shift is closed and its
code removed since 2026-10-05). The salary answer is the user's own words; the
`salary_min` search filter is only OFFERED as a starting point, never reused silently.

Nothing here is guessed. Where the user's own resume already names the answer (latest
job, school, LinkedIn), the form OFFERS it (`/profile/employer-answers/suggest`, read
from the uploaded PDF) and the person confirms.

HireDrop applies to US jobs only (Igor, 09-27: "мы только работаем с США"). So the
country question is "do you live in the United States?", and a No closes Start with its
own readiness row (`us_only`) rather than letting someone file US applications from
abroad. There is deliberately no country picker to choose Europe from.

EEO (gender, race, veteran, disability) is deliberately NOT here: those have a
deterministic "decline to answer" default, so a blank one is a filler miss, not missing
data, and asking a human for it before every start would be both wrong and intrusive.
"""

from __future__ import annotations

# (key, label, kind) — kind tells the form which control to draw.
#   text    free text
#   yesno   boolean; None = never answered (False is a real answer)
#   us_resident  yes/no, stored as country ("United States" or OUTSIDE_US)
QUESTIONS: tuple[tuple[str, str, str], ...] = (
    ("country", "Do you live in the United States?", "us_resident"),
    ("city", "City", "text"),
    ("state", "State", "text"),
    ("work_authorized_us", "Are you legally authorized to work in the United States?", "yesno"),
    ("needs_sponsorship", "Will you now or in the future require visa sponsorship?", "yesno"),
    ("current_title", "Most recent job title", "text"),
    ("current_employer", "Most recent employer", "text"),
    ("linkedin_url", "LinkedIn profile URL", "text"),
    ("school", "School or university", "text"),
    ("degree", "Degree", "text"),
    ("salary_expectation", "Salary expectation — what we tell employers who ask", "text"),
)

# WHICH CLIENTS MAY BE ASKED WHICH QUESTIONS.
# The three questions added on 2026-09-30 come with an "I don't have one" tickbox, and a
# website tab loaded before that day cannot draw it: it would show three bare text boxes
# and keep Start closed until the user typed SOMETHING — "N/A", "none", "negotiable" —
# which the fillers would then put on real applications. So a question is only counted
# as missing for a client that says it can ask it properly (`answers_ui`, sent by the
# website on /campaign/readiness, /campaign/start and the save). A client that says
# nothing is an old one and sees the list it has always seen.
ANSWERS_UI = 2
SINCE: dict[str, int] = {"school": 2, "degree": 2, "salary_expectation": 2}

# "I don't have one" IS the answer — the filler then hands a required field back
# honestly instead of inventing a URL or a university. flag -> the questions it answers.
OPT_OUT: dict[str, tuple[str, ...]] = {
    "no_linkedin": ("linkedin_url",),
    "no_degree": ("school", "degree"),
    "no_salary_expectation": ("salary_expectation",),
}
# The tickbox the form draws under the first question of each group. Sent with the
# question so no screen has to know which keys have an "I don't have one".
OPT_OUT_LABEL = {
    "no_linkedin": "I don't have a LinkedIn",
    "no_degree": "I don't have a college degree",
    "no_salary_expectation": "I'd rather not name a number",
}

US = "United States"
OUTSIDE_US = "Outside US"
_US = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}


def is_us(country: str | None) -> bool:
    return (country or "").strip().lower() in _US


def outside_us(profile: dict) -> bool:
    """Answered, and not the US — the one answer that closes Start outright."""
    country = str(profile.get("country") or "").strip()
    return bool(country) and not is_us(country)


def _opted_out(profile: dict, key: str) -> bool:
    return any(profile.get(flag) and key in keys for flag, keys in OPT_OUT.items())


def _row(key: str, label: str, kind: str) -> dict:
    row = {"key": key, "label": label, "kind": kind}
    for flag, keys in OPT_OUT.items():
        if key in keys:
            row["opt_out"] = {"flag": flag, "label": OPT_OUT_LABEL[flag]}
    return row


def _answered(profile: dict, key: str, kind: str) -> bool:
    if _opted_out(profile, key):
        return True
    value = profile.get(key)
    return value is not None if kind == "yesno" else bool(str(value or "").strip())


def suggestions(profile: dict) -> dict[str, str]:
    """What the question rows offer on their own — the user's settings, nothing else.

    What the RESUME says is offered by POST /profile/employer-answers/suggest, read from
    the PDF the person uploaded (modules/ai_resume_facts.py) and named in `from_resume`.
    It is not read here from `ats_structure` (10-06): that is the generated rewrite — its
    title need not be the one they held, and it can belong to an earlier upload — and a
    row that already carries a suggestion is one the form never asks /suggest about.
    """
    return own_suggestions(profile)


def own_suggestions(profile: dict) -> dict[str, str]:
    """Suggestions that come from the user's own SETTINGS rather than their resume.

    The salary floor they search with is the best guess at what they would tell an
    employer — but a guess, so it is offered in the form and never filed as the answer.
    """
    floor = profile.get("salary_min")
    if isinstance(floor, int | float) and not isinstance(floor, bool) and floor > 0:
        return {"salary_expectation": f"${int(floor):,} per year"}
    return {}


def missing(profile: dict, ui: int = ANSWERS_UI) -> list[dict]:
    """The unanswered questions, in form order. Empty list = ready.

    `ui` is what the asking client can draw (see SINCE): questions newer than it are
    left out — for that client they are not missing, they are not askable.
    """
    hints = suggestions(profile)
    out: list[dict] = []
    for key, label, kind in QUESTIONS:
        if SINCE.get(key, 1) > ui or _answered(profile, key, kind):
            continue
        row = _row(key, label, kind)
        if key in hints:
            row["suggestion"] = hints[key]
        out.append(row)
    return out


def form(profile: dict) -> list[dict]:
    """EVERY question with the answer already on file — what signup draws. `value` is in
    the form's own terms (yes/no for the country question), None/"" = not answered."""
    hints = suggestions(profile)
    out: list[dict] = []
    for key, label, kind in QUESTIONS:
        raw = profile.get(key)
        if kind == "us_resident":
            value = is_us(raw) if str(raw or "").strip() else None
        elif kind == "yesno":
            value = raw if isinstance(raw, bool) else None
        else:
            value = str(raw or "").strip()
        row = {**_row(key, label, kind), "value": value}
        if key in hints and not value:
            row["suggestion"] = hints[key]
        out.append(row)
    return out


# What POST /profile/employer-answers may write. Everything else in the body is ignored.
WRITABLE = tuple(k for k, _, _ in QUESTIONS) + tuple(OPT_OUT)
_MAX_TEXT = 200


def clean(body: dict) -> dict:
    """Keep only known keys, typed and bounded. Absent keys stay absent (partial save)."""
    out: dict = {}
    for key, _label, kind in QUESTIONS:
        if key not in body:
            continue
        raw = body[key]
        if kind == "yesno":
            if isinstance(raw, bool):
                out[key] = raw
        elif kind == "us_resident":
            if isinstance(raw, bool):
                out[key] = US if raw else OUTSIDE_US
        else:
            out[key] = str(raw or "").strip()[: (500 if key == "linkedin_url" else _MAX_TEXT)]
    for flag, keys in OPT_OUT.items():
        if flag in body:
            out[flag] = body[flag] is True
            # "I don't have a degree" over a school already on file (the ATS step, an
            # earlier answer) must not leave that school behind: any later write that
            # reset the flag would bring it back onto applications, unasked.
            if out[flag]:
                out.update(dict.fromkeys(keys, ""))
    return out
