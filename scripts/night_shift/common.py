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
# about the candidate. content.js answers these No, and so does the night shift.
#
# The pattern is NOT content.js's (`text message|sms|opt.?in|…|newsletter`): that one
# fires on the word alone, and an adversarial pass (09-30) showed what the word alone
# catches — "Have you managed email and SMS programs in Klaviyo?" was answered No for a
# marketing candidate without the model ever being asked, and `opt.?in` matches
# "adopting". An opt-in has a SHAPE: someone asking leave to contact you. A question
# about experience with the channel is not one.
_CHANNEL = (
    r"(?:\bsms\b|text messages?|\btexts?\b|whatsapp|phone calls?|\bcalls?\b|e-?mails?"
    r"|newsletters?|job alerts?|communications?|updates|marketing"
    r"|talent (?:community|network|pool))"
)
_OPT_IN_Q = re.compile(
    r"\bopt[- ]?in\b"
    r"|(?:would you like|do you (?:want|wish|agree|consent)|may we|can we|i (?:agree|consent)"
    r"|consent to|sign me up|subscribe|keep me (?:posted|updated|informed)|join (?:our|the))\b"
    rf".{{0,80}}{_CHANNEL}"
    rf"|\b(?:receive|get)\b.{{0,40}}{_CHANNEL}.{{0,60}}"
    r"\b(?:from us|from our|about (?:your|my|this) application|to the number|to my (?:phone|number|email))",
    re.I,
)
_ABOUT_EXPERIENCE = re.compile(
    r"\bexperience\b|\bdescribe\b|\bhow many\b|\byears?\b|\bcampaigns?\b|\bprograms?\b"
    r"|\bhave you (?:ever )?(?:managed|run|led|built|written|created|used|worked|launched|owned)",
    re.I,
)
_NO_OPT = re.compile(r"^\s*no\b", re.I)


def is_opt_in(label: str) -> bool:
    """Is this the board asking leave to contact the user (→ No), rather than a question
    about the candidate that merely mentions a channel?"""
    return bool(_OPT_IN_Q.search(label or "")) and not _ABOUT_EXPERIENCE.search(label or "")


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
# The school FIELD — its whole label, not any label that mentions a school. "Highest level
# of school completed" is a fixed list and "Did you graduate from college?" a yes/no;
# typing a university into those picked "Other" and handed the second one back.
_SCHOOL_FIELD = re.compile(
    r"^\W*(?:name of (?:your )?)?(?:school|university|college|institution)"
    r"(?:\s*(?:/|or|and)\s*(?:school|university|college|institution))?(?:\s+name)?\W*$",
    re.I,
)
_LOCATION_Q = re.compile(r"\b(location|city)\b", re.I)


def typeahead_kind(label: str) -> str | None:
    if _SCHOOL_FIELD.match(label or ""):
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
        # Where the person LIVES. `profile.location` is where they are searching, and a
        # relocating user would be declared a resident of the city they want to move to.
        city = (profile.get("city") or "").strip()
        return [city] if city else []
    return []


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def pick_typeahead(kind: str, query: str, options: list[str], profile: dict) -> str | None:
    """The one option that IS the typed fact, or None. Never the nearest-looking one:
    a search box answers "Portland" with both Oregon and Maine, "University of Hawaii"
    with every campus, and "MIT" with Smith College."""
    want = _squash(query)
    if not want or not options:
        return None
    if kind == "school":
        exact = [o for o in options if _squash(o) == want]
        if exact:
            return exact[0]
        # Beyond exact, only the same NAME with a note in brackets: "University of Hawaii
        # at Manoa (Honolulu)". Never a name that is a word-subset of ours or the other
        # way round — "Columbia College" (Missouri) is not "Columbia College Chicago", and
        # "Texas A&M University" is not its Corpus Christi campus.
        base = _squash(_BRACKETED.sub(" ", query))
        near = [o for o in options if base and _squash(_BRACKETED.sub(" ", o)) == base]
        return near[0] if len(near) == 1 else None

    from modules.job_location import parse_user_location

    user = parse_user_location(f"{query}, {profile.get('state') or ''}")
    hits = [o for o in options if _squash(o.split(",")[0]) == want]
    if not hits:
        return None
    known = [s for s in (user.get("state_name"), user.get("state_code")) if s]
    if known:
        # The user's state is on file: a hit that does not name it is another city with
        # the same name, however alone it stands on the list (Portland, Oregon is not
        # the answer for someone in Maine).
        named = [o for o in hits if any(s in [_squash(p) for p in o.split(",")[1:]] for s in known)]
        return named[0] if named else None
    # No state on file: only an unambiguous US city passes.
    usa = [o for o in hits if re.search(r"\b(united states|usa|us)\b", o, re.I)]
    pool = usa or hits
    return pool[0] if len(pool) == 1 else None


# ── How long one unwatched walk may go on ────────────────────────────────────────────
# What apply_one answers when the BOARD refused a submit we made: `invalid` (our own
# filling), `captcha_code` (Greenhouse asked for an emailed code), `flagged`, `unknown`.
REFUSALS = ("invalid", "captcha_code", "flagged", "unknown")
# Refused submits ONE RUN may spend, in total. Not "in a row": a hand-back or a knockout
# between two refusals is not good news about the board, and counting streaks let a
# simulated walk click Submit twenty times without one confirmed send. Each refusal is
# either a verification email to the user or an application we cannot prove was not
# received — three of those is the board (or our own filler) telling us to stop.
MAX_REFUSALS = 3


def walk_verdict(outcomes: list[str], max_jobs: int, capped: set[str], platforms: tuple) -> str:
    """Why the walk must stop after the latest outcome, or "" to keep going.

    This is the rule that decides how many real applications one unwatched run may send,
    and when it has to give up.
    """
    if sum(o in ("sent", "dry") for o in outcomes) >= max_jobs:
        return f"sent the {max_jobs} this run was allowed"
    if capped and capped >= set(platforms):
        return "the cap is reached on every board"
    refused = [o for o in outcomes if o in REFUSALS or o == "error"]
    if len(refused) >= MAX_REFUSALS:
        return (
            f"{len(refused)} submits refused or broken ({', '.join(refused)}) — not feeding it more"
        )
    return ""


# ── What the night shift answers from the PROFILE before any model is asked ───────────
# The shared answerer adapts what the résumé says. A salary expectation is not in a
# résumé, so the model made one up: live 09-30 it told three employers "$55,000–$65,000",
# "$60,000–$75,000" and "$70,000–$85,000" for a user whose own search floor is $100,000.
# content.js has always refused to do that ("NEVER invent a number here") and leaves the
# field for the human; this is the same rule on the server. The number comes from what
# the user told us to say (profile.salary_expectation, asked once at signup) or the
# question is not answered at all.
_PAY = (
    r"(?:salary|salaries|compensation|\bwages?\b|\bpay\b|\bpaid\b|\bOTE\b|\bincome\b|\bearnings\b"
    r"|\bremuneration\b|(?:hourly|pay|day) rate|rate of pay)"
)
_PAY_WORD = re.compile(_PAY, re.I)
# An ASK about pay: the word next to expect / desire / require / range, or the stock
# phrasings. The word alone is not one — "experience with compensation and benefits
# administration" got "100000" typed into a years-of-experience box.
_PAY_ASK = re.compile(
    rf"(?:expect\w*|desir\w*|requir\w*|target\w*|preferred|minimum|looking for|seeking)\b.{{0,40}}{_PAY}"
    rf"|{_PAY}.{{0,40}}\b(?:expect\w*|requir\w*|desir\w*|range|target\w*)"
    r"|how much (?:do|would|are) you (?:expect|want|like|need|looking)"
    rf"|what (?:is|are) your (?:\w+ ){{0,2}}{_PAY}",
    re.I,
)
# What they earn NOW is a different fact, and nobody told us that one either.
_PAY_CURRENT = re.compile(
    rf"\b(?:current|present|most recent|last|previous)\b.{{0,30}}{_PAY}", re.I
)
_NOT_ABOUT_MY_PAY = re.compile(
    r"\bexperience\b|\byears?\b|\bdescribe\b|\bhow many\b|\badministration\b|\bdesign\w*\b",
    re.I,
)
_MONEY = re.compile(r"\$\s?\d|\d\s?k\b|\d{2,3},\d{3}", re.I)
_WEBSITE_Q = re.compile(r"\b(?:website|portfolio|personal site|blog)\b", re.I)
_LINKEDIN_Q = re.compile(r"linkedin", re.I)
_OPEN_ABOVE = re.compile(r"\+|or more|and (?:up|above)|\babove\b|\bover\b|more than|at least", re.I)
_OPEN_BELOW = re.compile(r"\bunder\b|\bbelow\b|less than|up to", re.I)
_AMOUNT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([km])?\b", re.I)
_UNITS = (
    ("hour", re.compile(r"/\s*h(?:ou)?r\b|\bper hour\b|\bhourly\b|\ban hour\b|\bhrs?\b", re.I)),
    ("month", re.compile(r"/\s*mo(?:nth)?\b|\bper month\b|\bmonthly\b|\ba month\b", re.I)),
    (
        "year",
        re.compile(
            r"/\s*y(?:ea)?r\b|\bper year\b|\bper annum\b|\bannual\w*\b|\byearly\b|\ba year\b", re.I
        ),
    ),
)


def pay_question(label: str) -> str | None:
    """ "expectation", "current", or None when the label is not asking about the
    candidate's pay at all (and the ordinary answerer should take it)."""
    label = label or ""
    if not _PAY_WORD.search(label) or _NOT_ABOUT_MY_PAY.search(label):
        return None
    if _PAY_CURRENT.search(label):
        return "current"
    # A bare field label — "Salary", "Desired salary", "Compensation" — is the ask itself.
    return "expectation" if _PAY_ASK.search(label) or len(label.split()) <= 3 else None


def amounts(text: str) -> list[int]:
    """Every money figure in a phrase, a trailing k/m carried back over a range:
    "85-95k" is 85,000 to 95,000, not eighty-five dollars."""
    found = [
        (float(m.group(1).replace(",", "")), (m.group(2) or "").lower())
        for m in _AMOUNT.finditer(text or "")
    ]
    scale = {"k": 1_000, "m": 1_000_000, "": 1}
    out: list[int] = []
    for i, (value, unit) in enumerate(found):
        if not unit and value < 1000:
            unit = next((u for _v, u in found[i + 1 :] if u), "")
        if value:
            out.append(int(value * scale[unit]))
    return out


def parse_amount(text: str) -> int | None:
    """The first money figure in a phrase: "$100,000 per year" → 100000, "85k" → 85000."""
    found = amounts(text)
    return found[0] if found else None


def pay_unit(text: str, figures: list[int] | None = None) -> str | None:
    """hour / month / year, as written — else by size when the size leaves no doubt
    (nobody is paid $100,000 an hour or $35 a year). None = we cannot tell."""
    for unit, pattern in _UNITS:
        if pattern.search(text or ""):
            return unit
    figures = amounts(text) if figures is None else figures
    if figures and min(figures) >= 10_000:
        return "year"
    if figures and max(figures) <= 500:
        return "hour"
    return None


def salary_answer(
    profile: dict, options: list[str] | None = None, numeric: bool = False, label: str = ""
) -> str:
    """What the USER said to tell employers about pay, shaped for the control — or "".

    "" means nothing on file, they chose not to name a number, or their figure does not
    fit this control (other unit, no bracket that holds it): the field stays blank and a
    required one hands the form back. Never a figure of ours — and never the NEAREST
    bracket: "$70,000 – $85,000" is not what someone asking for $100,000 said.
    """
    stated = str(profile.get("salary_expectation") or "").strip()
    if not stated or profile.get("no_salary_expectation"):
        return ""
    figures = amounts(stated)
    amount = figures[0] if figures else None
    unit = pay_unit(stated, figures)
    if numeric:
        # A bare number carries no unit, so it is only typed where the field's unit is
        # the user's: salary boxes are annual unless the label says otherwise.
        wanted = pay_unit(label, []) or "year"
        return str(amount) if amount and unit == wanted else ""
    if not options:
        return stated
    if not amount or not unit:
        return ""
    # Brackets are half-open — "$75,000 – $100,000" then "$100,000 – $125,000" puts
    # $100,000 in the second — with one inclusive pass left for the top of the last one.
    for inclusive in (False, True):
        for option in options:
            bounds = amounts(option)
            if not bounds or pay_unit(option, bounds) != unit:
                continue
            low, high = min(bounds), max(bounds)
            if (
                len(bounds) >= 2
                and low <= amount
                and (amount < high or (inclusive and amount == high))
            ):
                return option
            # An open end: "$110,000+" holds every figure from there up, "Under $60,000"
            # every figure below.
            if len(bounds) == 1 and (
                (_OPEN_ABOVE.search(option) and amount >= low)
                or (_OPEN_BELOW.search(option) and amount < low)
            ):
                return option
    return ""


def number_from(reply: str, label: str = "") -> str:
    """The number a digits-only input should get out of the answerer's sentence.

    "Since 2019 I've run paid social … about 6 years" is 6, not 2019: the figure next to
    "years" wins, a calendar year is never an amount, and "how many years" cannot be 2019.
    """
    stated = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", reply or "", re.I)
    if stated:
        return stated.group(1)
    asks_years = bool(re.search(r"\byears?\b|how long", label or "", re.I))
    for m in re.finditer(r"\d+(?:\.\d+)?", reply or ""):
        value = float(m.group(0))
        if 1900 <= value <= 2100 or (asks_years and value > 60):
            continue
        return m.group(0)
    return ""


# ── Facts about the person the night shift knows without asking a model ────────────────
# "How did you hear about us?" — the model said "LinkedIn" (Tanium, dry walk 10-01). The
# night shift knows exactly how: it read the posting off the company's own board.
_SOURCE_Q = re.compile(
    r"how did you (?:first )?(?:hear|find|learn|come across)|where did you (?:first )?(?:hear|find|see|learn)"
    r"|how you heard|source of (?:this )?application",
    re.I,
)
_SOURCE_OPTIONS = (
    re.compile(r"career|company (?:web)?site|corporate (?:web)?site|our (?:web)?site", re.I),
    re.compile(r"job board|job site|online (?:job )?(?:board|posting)", re.I),
    re.compile(r"^\W*other\W*$", re.I),
)


def source_answer(options: list[str] | None = None) -> str:
    """Where this application came from, truthfully: the company's own careers board."""
    if not options:
        return "Your careers page."
    for wanted in _SOURCE_OPTIONS:
        hit = next((o for o in options if wanted.search(o)), None)
        if hit:
            return hit
    return ""


# "Are you located on the West Coast?" → Yes, for someone in Hawaii (Grafana Labs, dry
# walk 10-01). Where a person lives is on file; it is compared, not argued for.
_RESIDENCE_Q = re.compile(
    r"\b(?:are you|do you|will you be)\b.{0,30}\b(?:located|based|living|live|reside|residing)\b"
    r"|\bwithin \d+ ?(?:miles|mi|km|kms|kilometers)\b|\bcommut(?:e|able|ing)\b",
    re.I,
)


_IN_THE_US = re.compile(
    r"\b(?:located|based|living|live|reside|residing)\s+(?:currently\s+)?in\s+(?:the\s+)?"
    r"(?:u\.?s\.?a?\.?|united states)(?:\s+of america)?\s*[?.)]?\s*$",
    re.I,
)


def residence_answer(label: str, profile: dict, options: list[str] | None) -> str | None:
    """Yes / No to "are you located in X", from where the user lives.

    None = not this kind of question, or no Yes/No on offer (the caller carries on).
    "" = the question names a place we cannot compare ("the West Coast", "the Bay Area"):
    left for the person rather than guessed. A named US state that is not theirs is No.
    """
    if not options or not _RESIDENCE_Q.search(label or ""):
        return None
    yes = next((o for o in options if re.match(r"^\s*yes\b", o, re.I)), None)
    no = next((o for o in options if re.match(r"^\s*no\b", o, re.I)), None)
    if not yes or not no:
        return None
    from modules.job_location import _NAME_TO_CODE, _STATE_CODES, parse_user_location

    home = parse_user_location(f"{profile.get('city') or ''}, {profile.get('state') or ''}")
    city, code, name = home.get("city"), home.get("state_code"), home.get("state_name")
    if not (city or code):
        return ""
    low = " " + (label or "").lower() + " "
    # "…located IN the United States?" — the country itself is the place asked about.
    # ("on the West Coast of the US" is not: the US there only says which coast.)
    if _IN_THE_US.search(low):
        from modules.employer_answers import is_us

        return yes if is_us(profile.get("country")) else ""
    if city and re.search(rf"\b{re.escape(city)}\b", low):
        return yes
    # A state by its full name, or by its code written AS a code ("MA", not "ma" in a word).
    named = {n for n in _NAME_TO_CODE if re.search(rf"\b{re.escape(n)}\b", low)}
    named |= {
        _STATE_CODES[c.lower()]
        for c in re.findall(r"\b[A-Z]{2}\b", label or "")
        if c.lower() in _STATE_CODES
    }
    if named:
        return yes if name in named else no
    return ""


# A list too long to read: react-select shows the first page of ~250 countries, and a
# model handed "the options" picked from Afghanistan–Cambodia (Wikimedia, dry walk 10-01:
# "country of residence → Afghanistan"). These are typed from the profile instead.
_COUNTRY_Q = re.compile(r"\bcountry\b|\bcountries\b|\bnation\b", re.I)
_STATE_Q = re.compile(r"\bstate\b|\bprovince\b", re.I)
# "Where do you currently live?" over a long list: states on one form, countries on the
# next. Both spellings of the answer are tried; only an exact row is ever taken.
_LIVES_Q = re.compile(r"\bwhere do you (?:currently )?(?:live|reside)\b|\bresidence\b", re.I)


def long_list_fact(label: str, profile: dict) -> list[str]:
    """What to type into a dropdown whose list we cannot see whole — ways of writing the
    same fact, most likely first. [] = no fact for this field: leave it for the person."""
    from modules.employer_answers import is_us
    from modules.job_location import parse_user_location

    if _COUNTRY_Q.search(label or ""):
        return ["United States", "USA"] if is_us(profile.get("country")) else []
    home = parse_user_location(f"x, {profile.get('state') or ''}")
    state = [s.title() for s in (home.get("state_name"),) if s]
    if _STATE_Q.search(label or ""):
        return state
    if _LIVES_Q.search(label or ""):
        return state + (["United States", "USA"] if is_us(profile.get("country")) else [])
    return []


def pick_from_long_list(query: str, options: list[str]) -> str | None:
    """The filtered option that IS the typed fact: equal text, else — for a country —
    the plain long form, never a territory that merely starts the same way."""
    want = _squash(query)
    exact = [o for o in options if _squash(o) == want]
    if exact:
        return exact[0]
    if want in ("united states", "usa"):
        return next(
            (
                o
                for o in options
                if _squash(o) in ("united states of america", "usa", "us", "u s", "u s a")
            ),
            None,
        )
    return None


def answer(
    label: str, job: dict, profile: dict, options: list[str] | None = None, numeric: bool = False
) -> str:
    """The night shift's ONE way to answer a form question.

    What we KNOW first (pay, website, where the application came from, where the person
    lives), then the shared answerer in its unattended mode. `numeric` is for inputs
    that accept digits only — Ashby's "How many years…" is `type=number`, and the prose
    the answerer returns ("About 5 years, spanning…") cannot be typed into it at all.
    """
    label = label or ""
    pay = pay_question(label)
    if pay == "expectation":
        return salary_answer(profile, options, numeric, label)
    if pay == "current":
        return ""
    # "Website" is the candidate's own site or portfolio. The answerer, having none to
    # give, repeated the LinkedIn URL the form had already been given one field above.
    if not options and _WEBSITE_Q.search(label) and not _LINKEDIN_Q.search(label):
        return str(profile.get("portfolio_url") or "").strip()
    if _SOURCE_Q.search(label):
        return source_answer(options)
    where = residence_answer(label, profile, options)
    if where is not None:
        return where
    # Nobody reads a night-shift answer before the employer does: the answerer runs in
    # its unattended mode, where a question about the person's circumstances that the
    # résumé and the profile do not settle comes back empty instead of argued.
    reply = answer_screener_question(
        label, job=job, profile=profile, options=options, unattended=True
    )
    # The net under pay_question: a pay phrasing it did not recognise still reached the
    # model, and a money figure in the reply is one the user never gave.
    invented_pay = (
        bool(reply)
        and not options
        and _PAY_WORD.search(label)
        and not _NOT_ABOUT_MY_PAY.search(label)
        and _MONEY.search(reply)
    )
    if invented_pay:
        return ""
    if numeric and reply:
        return number_from(reply, label)
    return reply
