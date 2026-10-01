"""Rules every night-shift board adapter shares (Greenhouse in executor.py, Ashby in
ashby.py). One place, so the two boards cannot disagree about what a demographic
question is or which honest answer disqualifies the candidate."""

import re

from modules.ai_question_answer import answer_screener_question

# Voluntary self-identification questions, and the neutral way out of them. We decline
# these on principle rather than let a model infer a person's gender or race from a CV.
_DEMOGRAPHIC_Q = re.compile(
    r"gender|race|ethnic|sexual orientation|lgbt|transgender|disability|veteran"
    # "Are you Hispanic or Latinx?" names no "ethnicity" — it reached the model, which
    # happened to decline. Declining must not depend on the model's mood.
    r"|hispanic|latin[oax]\b|self.?identif|demographic|pronoun",
    re.I,
)
_DECLINE_OPT = re.compile(
    r"don'?t wish|do not wish|decline|prefer not|rather not|not to (?:answer|disclose)"
    r"|no answer|not wish to (?:answer|identify)",
    re.I,
)


# "May we text / email you?" — the board asking for a channel, not the employer asking
# about the candidate. The pattern is content.js's own (the Marketing/SMS opt-in rule),
# kept identical on purpose: one user must not get two different answers to the same
# question depending on which executor reached the form.
_OPT_IN_Q = re.compile(
    r"text message|sms|opt.?in|receive (calls|messages|texts)"
    r"|talent (community|network|pool)|newsletter",
    re.I,
)
_NO_OPT = re.compile(r"^\s*no\b", re.I)


def _words(text: str) -> list[str]:
    return re.sub(r"\s+", " ", text or "").strip().lower().split()


def same_value(wanted: str, held: str, options: list[str] | tuple = ()) -> bool:
    """Does the widget hold the option we clicked?

    Equal text is the answer. The one tolerance is a control that shows LESS than its
    option said — the phone-country option "United States +1" is held as "+1" — so a
    held text that is the whole-word start or end of the wanted one also counts.
    Never the other way round, and never loose containment: "No" is inside "None of the
    above" and "Not sure", "Yes" starts "Yes, with sponsorship", and those are exactly
    the wrong clicks this check exists to catch. A held text that is some OTHER option,
    word for word, is that other option — whatever it happens to be a prefix of.
    """
    want, have = _words(wanted), _words(held)
    if not want or not have:
        return False
    if want == have:
        return True
    if any(_words(o) == have for o in options if _words(o) != want):
        return False
    return len(have) < len(want) and (want[: len(have)] == have or want[-len(have) :] == have)


def log(msg: str) -> None:
    print(msg, flush=True)


# KNOCKOUT: a screener whose honest answer rules the candidate out. "Are you located in
# X / can you work from our office / do you have N years" is the employer's filter, not
# a preference — answering "no" and sending anyway spends a daily slot on a certain
# rejection. Sponsorship questions are the inverse ("No" is the good answer) and never
# knock out.
_HARD_Q = re.compile(
    r"are you (?:currently )?(?:located|based|living)|able to (?:work|commute|relocate)"
    r"|do you (?:have|possess).{0,40}(?:years|experience|degree|license|certification)"
    r"|legally (?:authorized|eligible)|eligible to work|willing to relocate"
    r"|can you (?:work|start|commute)"
    # A language the role is built on. Live 09-30: "Influencer Marketing Coordinator -
    # Bilingual Spanish" asked "Do you have professional fluency in both Spanish and
    # English?", the honest answer was No, and nothing here called that a knockout.
    r"|(?:are you|do you (?:have|speak|possess)).{0,40}(?:fluen|bilingual|proficien)",
    re.I,
)
_NEGATIVE_A = re.compile(r"^\s*(no|nope|n/a|not\b|i am not|i'm not|i do not|i don't)\b", re.I)


def is_knockout(question: str, answer: str) -> bool:
    if not question or not answer or re.search(r"sponsor|visa", question, re.I):
        return False
    return bool(_HARD_Q.search(question) and _NEGATIVE_A.match(answer))


# TYPEAHEAD FIELDS: the answer is a FACT from the profile, typed into a search box.
# Greenhouse renders "Location (City)" and "School" as react-selects whose menu is fed by
# a search endpoint — empty (or an arbitrary first page) until someone types. Live
# 2026-09-30: the first server-side submit filled a whole DoorDash form and stopped on
# exactly these two. Neither is a question for a model: picking a university out of the
# first forty rows of an alphabetical list is how a wrong school reaches an employer.
_SCHOOL_Q = re.compile(r"\b(school|university|college|institution)\b", re.I)
_LOCATION_Q = re.compile(r"\b(location|city)\b", re.I)


def typeahead_kind(label: str) -> str | None:
    if _SCHOOL_Q.search(label or ""):
        return "school"
    if _LOCATION_Q.search(label or ""):
        return "location"
    return None


def typeahead_queries(kind: str, profile: dict) -> list[str]:
    """What to type, in order. [] = the profile holds no fact for this field, so it is a
    hand-back — never a guess."""
    if kind == "school":
        school = (profile.get("school") or "").strip()
        if profile.get("no_degree") or not school:
            return []
        # Greenhouse's own list carries an "Other" row for schools it does not know —
        # the honest answer when the real one is not offered.
        return [school, "Other"]
    if kind == "location":
        city = (profile.get("city") or "").strip() or (
            (profile.get("location") or "").split(",")[0].strip()
        )
        return [city] if city and city.lower() not in ("remote", "anywhere") else []
    return []


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def pick_typeahead(kind: str, query: str, options: list[str], profile: dict) -> str | None:
    """The one option that IS the typed fact, or None. Never the nearest-looking one:
    a search box answers "Portland" with both Oregon and Maine, and "University of
    Hawaii" with every campus."""
    want = _squash(query)
    if not want or not options:
        return None
    if kind == "school":
        exact = [o for o in options if _squash(o) == want]
        if exact:
            return exact[0]
        # One — and only one — option that contains the whole name (or is contained in
        # it): "University of Hawaii at Manoa" for "University of Hawaii at Manoa (UH)".
        near = [o for o in options if want in _squash(o) or _squash(o) in want]
        return near[0] if len(near) == 1 else None

    from modules.job_location import parse_user_location

    user = parse_user_location(f"{query}, {profile.get('state') or ''}")
    hits = [o for o in options if _squash(o.split(",")[0]) == want]
    if not hits:
        return None
    state = [
        o
        for o in hits
        if any(
            s and s in [_squash(p) for p in o.split(",")[1:]]
            for s in (user.get("state_name"), user.get("state_code"))
        )
    ]
    if state:
        return state[0]
    # No state on file (or none of the hits names it): only an unambiguous city passes.
    usa = [o for o in hits if re.search(r"\b(united states|usa|us)\b", o, re.I)]
    pool = usa or hits
    return pool[0] if len(pool) == 1 else None


# ── How long one unwatched walk may go on ────────────────────────────────────────────
# What apply_one answers when the BOARD refused a submit we made: `invalid` (our own
# filling), `captcha_code` (Greenhouse asked for an emailed code), `flagged`, `unknown`.
REFUSALS = ("invalid", "captcha_code", "flagged", "unknown")
# One refusal is a data point. A streak is the board telling us something every further
# application would hear too — our address's score, or a filling bug all these forms
# share — so the walk stops instead of spending the user's name on it. A filler that
# crashes three forms running is the same signal from our side.
MAX_REFUSALS_IN_A_ROW = 3


def walk_verdict(outcomes: list[str], max_jobs: int, capped: set[str], platforms: tuple) -> str:
    """Why the walk must stop after the latest outcome, or "" to keep going.

    This is the rule that decides how many real applications one unwatched run may send,
    and when it has to give up.
    """
    if sum(o in ("sent", "dry") for o in outcomes) >= max_jobs:
        return f"sent the {max_jobs} this run was allowed"
    if capped and capped >= set(platforms):
        return "the cap is reached on every board"
    streak = 0
    for o in reversed(outcomes):
        if o not in REFUSALS and o != "error":
            break
        streak += 1
    if streak >= MAX_REFUSALS_IN_A_ROW:
        return f"{streak} in a row ended in {outcomes[-1]} — not feeding it more"
    return ""


# ── What the night shift answers from the PROFILE before any model is asked ───────────
# The shared answerer adapts what the résumé says. A salary expectation is not in a
# résumé, so the model made one up: live 09-30 it told three employers "$55,000–$65,000",
# "$60,000–$75,000" and "$70,000–$85,000" for a user whose own search floor is $100,000.
# content.js has always refused to do that ("NEVER invent a number here") and leaves the
# field for the human; this is the same rule on the server. The number comes from what
# the user told us to say (profile.salary_expectation, asked once at signup) or the
# question is not answered at all.
_SALARY_Q = re.compile(
    r"salary|compensation|\bwage|pay (?:range|rate|expectation|requirement)"
    r"|expected (?:pay|rate)|desired (?:pay|rate)|hourly rate|rate expectation",
    re.I,
)
_WEBSITE_Q = re.compile(r"\b(?:website|portfolio|personal site|blog)\b", re.I)
_LINKEDIN_Q = re.compile(r"linkedin", re.I)
_OPEN_ABOVE = re.compile(r"\+|or more|and (?:up|above)|\babove\b|\bover\b|more than|at least", re.I)
_OPEN_BELOW = re.compile(r"\bunder\b|\bbelow\b|less than|up to", re.I)
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k\b|m\b)?", re.I)


def parse_amount(text: str) -> int | None:
    """The first money figure in a phrase: "$100,000 per year" → 100000, "85k" → 85000."""
    m = _AMOUNT.search(text or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    return int(value * (1_000 if unit == "k" else 1_000_000 if unit == "m" else 1))


def _all_amounts(text: str) -> list[int]:
    out = []
    for m in _AMOUNT.finditer(text or ""):
        value = parse_amount(m.group(0))
        if value:
            out.append(value)
    return out


def salary_answer(profile: dict, options: list[str] | None = None, numeric: bool = False) -> str:
    """What the USER said to tell employers about pay, shaped for the control — or "".

    "" means nothing on file, or they chose not to name a number: the field stays blank
    and a required one hands the form back. Never a figure of ours.
    """
    stated = str(profile.get("salary_expectation") or "").strip()
    if not stated or profile.get("no_salary_expectation"):
        return ""
    amount = parse_amount(stated)
    if numeric:
        return str(amount) if amount else ""
    if not options:
        return stated
    if not amount:
        return ""
    # A list of ranges: the one that contains the figure, else the nearest bound — but
    # never across units (an annual figure against hourly brackets is not "nearest").
    best: tuple[float, str] | None = None
    for option in options:
        bounds = _all_amounts(option)
        if not bounds:
            continue
        if len(bounds) >= 2 and min(bounds) <= amount <= max(bounds):
            return option
        # An open end: "$110,000+" holds every figure above it, "Under $60,000" every
        # figure below — distance to the bound is the wrong measure for these.
        if len(bounds) == 1 and (
            (_OPEN_ABOVE.search(option) and amount >= bounds[0])
            or (_OPEN_BELOW.search(option) and amount <= bounds[0])
        ):
            return option
        gap = min(abs(b - amount) for b in bounds) / amount
        if best is None or gap < best[0]:
            best = (gap, option)
    return best[1] if best and best[0] <= 0.5 else ""


def answer(
    label: str, job: dict, profile: dict, options: list[str] | None = None, numeric: bool = False
) -> str:
    """The night shift's ONE way to answer a form question.

    Profile facts first (pay), then the shared answerer. `numeric` is for inputs that
    accept digits only — Ashby's "How many years…" is `type=number`, and the prose the
    answerer returns ("About 5 years, spanning…") cannot be typed into it at all.
    """
    if _SALARY_Q.search(label or ""):
        return salary_answer(profile, options, numeric)
    # "Website" is the candidate's own site or portfolio. The answerer, having none to
    # give, repeated the LinkedIn URL the form had already been given one field above.
    if not options and _WEBSITE_Q.search(label or "") and not _LINKEDIN_Q.search(label or ""):
        return str(profile.get("portfolio_url") or "").strip()
    reply = answer_screener_question(label, job=job, profile=profile, options=options)
    if numeric and reply:
        m = re.search(r"\d+(?:\.\d+)?", reply)
        return m.group(0) if m else ""
    return reply
