"""Early salary filter (GLOBAL_PLAN "salary min/max at launch").

Applied BEFORE the expensive steps (AI scoring / fit / cover letter) and before a slot is
spent: a job whose listed pay falls outside the user's range is dropped, so we never pay
model tokens or an application on it.

The pool has NO salary column (project_salary_not_stored), so pay is whatever we can read
out of the posting text — and the measurement that sized this (scripts/measure_salary_fill.py,
09-26) says only ~12% of rows carry pay we can read at all: 40% on Indeed, 7% on Greenhouse,
0% on Ashby/Workday. Two consequences shape this module:

  1. "Not listed" must PASS, or the deck collapses — the same measurement put a
     listed-only deck at 6% of its size and 1 of 124 real applications. Rejecting only
     what we can READ is the rule; `salary_listed_only` is the user's own opt-out.
  2. Because only a parsed number can reject a job, a FALSE parse is the only way this
     module can hurt someone. Precision beats recall here, and the live pool is full of
     traps: "We recently raised a $355M Series C", "budgets will vary from $50K to $30M+",
     "$650 Health Benefit Stipend" right after the words "hourly compensation". So an
     amount counts as pay only when the text says it is pay:
       - a pay cue (salary / base pay / compensation / wage / hourly …) within ~a sentence
         before it, or an explicit period marker after it ("per year", "/hr");
       - a period, taken from the marker after the amount or the nearest period word
         before it, defaulting to annual;
       - and a per-period sanity range, so $650 cannot be an hourly rate and $450M cannot
         be an annual salary.
"""

from __future__ import annotations

import re

# Annualisation factors per period (40h × 52w, 5 days × 52w — the standard figures).
_ANNUAL_FACTOR = {"year": 1, "month": 12, "week": 52, "day": 260, "hour": 2080}

# Plausible RAW amounts per period, checked before annualising. This is what separates a
# stipend from an hourly rate and a company's revenue from a salary.
_PLAUSIBLE_RAW = {
    "year": (15_000, 1_500_000),
    # $60k/month = $720k/year, above any salary we would see. Tighter than it looks on
    # purpose: Indeed serves occasional garbage chips ("$1 - $100,000 a month") and a wide
    # band turns those into a six-figure "fact".
    "month": (1_200, 60_000),
    "week": (250, 30_000),
    "day": (50, 5_000),
    "hour": (7, 400),
}

# $ amount: "$120k", "$120,000", "$65", "$62.5k", "$ 87,188.40"
_AMOUNT = r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kKmM])?"
# "$X - $Y" / "$X to $Y" ranges (en/em dashes included; the second $ is optional because
# postings write "$75,000 - 100,000" too).
_RANGE_RE = re.compile(
    _AMOUNT
    + r"\s*(?:-|–|—|to)\s*\$?\s*"
    + r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*([kKmM])?"
)
_SINGLE_RE = re.compile(_AMOUNT)

# Words that make a dollar amount pay. Checked in the window BEFORE the amount; a funding
# round or a project budget has none of them.
_PAY_CUE_RE = re.compile(
    r"\b(salar\w*|compensation|base\s+pay|pay\s+(?:range|rate|scale|transparency)|pays?|paid"
    r"|wage\w*|hourly|per\s+hour|annual\s+pay|earn\w*|remuneration|OTE|stipend|rate\s+of)\b",
    re.IGNORECASE,
)
_CUE_WINDOW = 160  # chars ≈ one sentence of context before the amount

# A period stated right AFTER the amount: "per year", "/hr", "annually", "USD per month".
_PERIOD_AFTER_RE = re.compile(
    # Several filler words can stand between the amount and the period: "$6,500 USD gross
    # per month" (live, Greenhouse 09-26) — so the filler repeats instead of appearing once.
    r"^[\s\)\],;:.]*(?:(?:usd|cad|gross|net|base|pay|salary)[\s\-–—]+)*"
    r"(?:(annually|annualized|yearly|hourly|monthly|weekly|daily)"
    r"|(?:per\s+|/\s*|an?\s+|each\s+)(hour|hrs?|year|yr|annum|month|mo|week|wk|day))"
    # NOT \b at the end: Indeed's harvested card runs the pay chip straight into the next
    # chip — "$210,000 - $250,000 a yearFull-time403(b)" — and a word boundary never fires
    # between "year" and "Full". That one character cost us the single most common real-pay
    # shape in the pool: 319 of 900 Indeed rows (measured 09-26).
    # (?-i:…) is load-bearing: under IGNORECASE a bare [a-z] also matches "F", which made
    # this lookahead reject every chip instead of accepting it.
    r"(?-i:(?![a-z]))",
    re.IGNORECASE,
)
# …or stated BEFORE it ("hourly compensation of", "annual base salary"). Last one wins.
_PERIOD_BEFORE_RE = re.compile(
    r"\b(hourly|per\s+hour|/\s*hr|monthly|per\s+month|/\s*mo|weekly|per\s+week"
    r"|daily|per\s+day|annually|annual|yearly|per\s+year|/\s*yr|salar\w*|base\s+pay)\b",
    re.IGNORECASE,
)
_PERIOD_WORDS = {
    "hour": "hour",
    "hr": "hour",
    "hrs": "hour",
    "hourly": "hour",
    "per hour": "hour",
    "/hr": "hour",
    "year": "year",
    "yr": "year",
    "annum": "year",
    "annually": "year",
    "annualized": "year",
    "yearly": "year",
    "annual": "year",
    "per year": "year",
    "/yr": "year",
    "month": "month",
    "mo": "month",
    "monthly": "month",
    "per month": "month",
    "/mo": "month",
    "week": "week",
    "wk": "week",
    "weekly": "week",
    "per week": "week",
    "day": "day",
    "daily": "day",
    "per day": "day",
}


def _period_key(raw: str) -> str | None:
    key = re.sub(r"\s+", " ", raw.strip().lower())
    if key in _PERIOD_WORDS:
        return _PERIOD_WORDS[key]
    if key.startswith("salar") or key == "base pay":
        return "year"  # "salary" names the period as surely as "per year" does
    return None


def _period_for(before: str, after: str) -> str:
    """The period the posting states for this amount; annual when it states none."""
    m = _PERIOD_AFTER_RE.match(after)
    if m:
        return _period_key(m.group(1) or m.group(2)) or "year"
    hits = list(_PERIOD_BEFORE_RE.finditer(before))
    for hit in reversed(hits):
        key = _period_key(hit.group(1))
        if key:
            return key
    return "year"


def _to_number(raw: str, suffix: str | None) -> float:
    val = float(raw.replace(",", ""))
    if suffix and suffix.lower() == "k":
        val *= 1_000
    elif suffix and suffix.lower() == "m":
        val *= 1_000_000
    return val


def _annual(val: float, period: str) -> float | None:
    """Annualise a raw amount, or None when it is not a plausible wage for that period."""
    lo, hi = _PLAUSIBLE_RAW[period]
    if not lo <= val <= hi:
        return None
    return val * _ANNUAL_FACTOR[period]


def _reads_as_pay(text: str, start: int) -> bool:
    """Does the posting call this amount pay? A cue in the sentence before it, or an
    explicit period marker straight after it. Without either, it is a funding round,
    a project budget or a customer number — 88% of the dollar amounts in our pool."""
    return bool(_PAY_CUE_RE.search(text[max(0, start - _CUE_WINDOW) : start]))


def parse_salary(text: str) -> tuple[int, int] | None:
    """Extract an annual USD (lo, hi) from free text, or None if nothing reads as pay.

    Ranges win over single amounts; a single amount yields lo == hi. First candidate that
    reads as pay AND lands inside its period's plausible band wins — a posting that
    mentions a $355M round before stating a salary must not be read from the round.
    """
    if not text:
        return None

    for m in _RANGE_RE.finditer(text):
        before = text[: m.start()]
        after = text[m.end() : m.end() + 28]
        if not (_reads_as_pay(text, m.start()) or _PERIOD_AFTER_RE.match(after)):
            continue
        period = _period_for(before[-_CUE_WINDOW:], after)
        lo = _annual(_to_number(m.group(1), m.group(2)), period)
        hi = _annual(_to_number(m.group(3), m.group(4)), period)
        if lo is None or hi is None:
            continue
        if lo > hi:
            lo, hi = hi, lo
        return int(lo), int(hi)

    for m in _SINGLE_RE.finditer(text):
        before = text[: m.start()]
        after = text[m.end() : m.end() + 28]
        if not (_reads_as_pay(text, m.start()) or _PERIOD_AFTER_RE.match(after)):
            continue
        period = _period_for(before[-_CUE_WINDOW:], after)
        val = _annual(_to_number(m.group(1), m.group(2)), period)
        if val is None:
            continue
        return int(val), int(val)

    return None


def passes_salary(
    job: dict, salary_min: int | None, salary_max: int | None, listed_only: bool = False
) -> bool:
    """True if the job survives the user's salary filter.

    Rules (settled in GLOBAL_PLAN):
    - user set no bounds and not listed_only → everything passes;
    - salary unparseable/absent → passes UNLESS listed_only;
    - parsed [lo, hi] must OVERLAP [salary_min or 0, salary_max or ∞].
    """
    if not salary_min and not salary_max and not listed_only:
        return True

    text = " ".join(
        str(job.get(k) or "") for k in ("salary", "compensation", "description", "title")
    )
    parsed = parse_salary(text)
    if parsed is None:
        return not listed_only

    lo, hi = parsed
    if salary_min and hi < salary_min:
        return False
    return not (salary_max and lo > salary_max)


def filter_by_salary(jobs: list[dict], profile: dict) -> tuple[list[dict], int]:
    """Apply the user's salary prefs to a discovery batch. Returns (kept, dropped_count)."""
    salary_min = profile.get("salary_min") or None
    salary_max = profile.get("salary_max") or None
    listed_only = bool(profile.get("salary_listed_only"))
    if not salary_min and not salary_max and not listed_only:
        return jobs, 0
    kept = [j for j in jobs if passes_salary(j, salary_min, salary_max, listed_only)]
    return kept, len(jobs) - len(kept)
