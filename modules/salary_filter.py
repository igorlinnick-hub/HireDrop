"""Early salary filter (GLOBAL_PLAN "salary min/max at launch").

Applied BEFORE the expensive steps (AI scoring / fit / cover letter): a job whose listed
pay falls outside the user's range is dropped at discovery time, so we never spend model
tokens or an application slot on it.

Parsing is deliberately conservative (v1): we act only on pay we can read with confidence
(``$120k``, ``$120,000 - $150,000``, ``$65/hr``). Anything unparseable counts as "salary
not listed" → the job is INCLUDED unless the user set ``salary_listed_only`` — dropping
every posting without a printed salary would kill most of the GH/Lever supply.
"""

from __future__ import annotations

import re

# Hours per work year for hourly→annual conversion (40h × 52w — the standard figure).
_HOURS_PER_YEAR = 2080

# Sanity bounds after normalising to annual USD: outside these the "salary" is almost
# certainly a parse artifact (a $500 bonus, a $2M contract figure), not base pay.
_MIN_PLAUSIBLE = 15_000
_MAX_PLAUSIBLE = 1_500_000

# $ amount: "$120k", "$120,000", "$65", "$62.5k"
_AMOUNT = r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kK])?"
# "$X - $Y" / "$X to $Y" ranges (en/em dashes included)
_RANGE_RE = re.compile(_AMOUNT + r"\s*(?:-|–|—|to)\s*" + _AMOUNT)
_SINGLE_RE = re.compile(_AMOUNT)
# Hourly marker within a short window after the amount
_HOURLY_RE = re.compile(r"^\s*(?:/|per\s+|an\s+)?\s*(?:hr|hour|hourly)", re.IGNORECASE)


def _to_number(raw: str, k_flag: str | None) -> float:
    val = float(raw.replace(",", ""))
    if k_flag:
        val *= 1_000
    return val


def _annualize(val: float, tail: str) -> float:
    """Convert to annual USD using the text right after the match (hourly detection)."""
    if _HOURLY_RE.match(tail):
        return val * _HOURS_PER_YEAR
    return val


def parse_salary(text: str) -> tuple[int, int] | None:
    """Extract an annual USD (lo, hi) from free text, or None if nothing plausible.

    Ranges win over single amounts. A single amount yields lo == hi.
    """
    if not text:
        return None

    m = _RANGE_RE.search(text)
    if m:
        lo = _annualize(_to_number(m.group(1), m.group(2)), text[m.end() : m.end() + 12])
        hi = _annualize(_to_number(m.group(3), m.group(4)), text[m.end() : m.end() + 12])
        if lo > hi:
            lo, hi = hi, lo
        if lo >= _MIN_PLAUSIBLE and hi <= _MAX_PLAUSIBLE:
            return int(lo), int(hi)

    for m in _SINGLE_RE.finditer(text):
        val = _annualize(_to_number(m.group(1), m.group(2)), text[m.end() : m.end() + 12])
        if _MIN_PLAUSIBLE <= val <= _MAX_PLAUSIBLE:
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
