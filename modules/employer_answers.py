"""The questions employers almost always ask — answered once, before the first Start.

Measured 2026-09-27 over 30 days of hand-backs (`activity_db.handback_stats`): the forms
that stopped a run stopped on the same handful of questions — country 10, sponsorship 8,
city 7, work authorization 7, recent job title 5, recent employer 5, LinkedIn 4. Almost
all of them already had a profile field; people simply never filled it, so the filler
reached 99% of a form and handed it back. Igor, 09-27: block Start until every one is
answered — "один раз ответил и всё готово".

This list is the ONE authority: `/campaign/readiness` shows what is missing from it,
`/campaign/start` refuses on it, and the dashboard renders its form from what the server
returns — so the three can never disagree about which questions count.

EEO (gender, race, veteran, disability) is deliberately NOT here: those have a
deterministic "decline to answer" default, so a blank one is a filler miss, not missing
data, and asking a human for it before every start would be both wrong and intrusive.
"""

from __future__ import annotations

# (key, label, kind) — kind tells the form which control to draw.
#   text    free text
#   yesno   boolean; None = never answered (False is a real answer)
#   country free text drawn with a country picker
QUESTIONS: tuple[tuple[str, str, str], ...] = (
    ("country", "Country you live in", "country"),
    ("city", "City", "text"),
    ("state", "State", "text"),
    ("work_authorized_us", "Are you legally authorized to work in the United States?", "yesno"),
    ("needs_sponsorship", "Will you now or in the future require visa sponsorship?", "yesno"),
    ("current_title", "Most recent job title", "text"),
    ("current_employer", "Most recent employer", "text"),
    ("linkedin_url", "LinkedIn profile URL", "text"),
)

_US = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}


def is_us(country: str | None) -> bool:
    return (country or "").strip().lower() in _US


def missing(profile: dict) -> list[dict]:
    """The unanswered questions, in form order. Empty list = ready."""
    out: list[dict] = []
    for key, label, kind in QUESTIONS:
        # "State" only means something for a US address; elsewhere forms ask for a
        # region inconsistently and a blank one is not what stops them.
        if key == "state" and not is_us(profile.get("country")):
            continue
        # Not everyone has a LinkedIn. Saying so IS the answer — the filler then hands
        # a required LinkedIn field back honestly instead of inventing a URL.
        if key == "linkedin_url" and profile.get("no_linkedin"):
            continue
        value = profile.get(key)
        answered = value is not None if kind == "yesno" else bool(str(value or "").strip())
        if not answered:
            out.append({"key": key, "label": label, "kind": kind})
    return out


# What POST /profile/employer-answers may write. Everything else in the body is ignored.
WRITABLE = tuple(k for k, _, _ in QUESTIONS) + ("no_linkedin",)
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
        else:
            out[key] = str(raw or "").strip()[: (500 if key == "linkedin_url" else _MAX_TEXT)]
    if "no_linkedin" in body:
        out["no_linkedin"] = body["no_linkedin"] is True
    return out
