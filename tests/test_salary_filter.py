"""Salary filter (the gate before AI scoring and before a slot is spent).

The fixtures in the REAL_* tables are COPIED OUT OF THE LIVE POOL (2026-09-26, via
scripts/measure_salary_fill.py) — not composed here. That matters twice over: a made-up
fixture already passed 12 checks and then failed on 6 live forms
(feedback_fixtures_must_be_captured), and this module's only way to hurt a user is to read
pay where there is none, which is exactly what invented fixtures never show.
"""

import pytest

from modules.salary_filter import filter_by_salary, parse_salary, passes_salary

# ── real postings that DO state pay ───────────────────────────────────────────

REAL_PAY = [
    # Greenhouse, monthly pay with two filler words before the period marker.
    (
        "The salary range for this role is negotiable, the range being $5,000 - $6,500 "
        "USD gross per month.",
        (60_000, 78_000),
    ),
    (
        "The salary range for this role is $5,000 - $8,000 per month (Gross in USD)",
        (60_000, 96_000),
    ),
    # The most common shape in our pool: Greenhouse pay-transparency block.
    (
        "#LI-Remote Pay Transparency: The base pay for this role is: $100,602- $132,040 per year.",
        (100_602, 132_040),
    ),
    # Cents and a space after the $ — both live.
    (
        "The base pay for this role in the states of California is: $ 87,188.40- $114,434.78 "
        "per year.",
        (87_188, 114_434),
    ),
    # Indeed, hourly.
    (
        "provide full-time Team Members with an industry-leading benefits package, including: "
        "Compensation: $17.00 per hour, plus commission and bonuses",
        (35_360, 35_360),
    ),
    # Indeed's own pay chip — the most common real-pay shape in the pool (319 of 900 rows,
    # 09-26). The chips are concatenated with no whitespace, which is why the period
    # lookahead must survive "a yearFull-time".
    ("$210,000 - $250,000 a yearFull-time403(b)401(k)Health insurance", (210_000, 250_000)),
    ("$54,000 - $56,000 a yearFull-time", (54_000, 56_000)),
    ("$32 - $42 an hourFull-timeMonday to Friday +1401(k) matching", (66_560, 87_360)),
    # Travel-nurse weekly rate, also straight off an Indeed card.
    ("Up to $2,988 a weekFull-time +2Day shift +1Referral program", (155_376, 155_376)),
    # Lever.
    (
        "The anticipated annual salary for this role will range from $75,000 - $100,000, based "
        "on a variety of factors unique to each candidate",
        (75_000, 100_000),
    ),
]

# ── real postings whose dollar amounts are NOT pay ────────────────────────────

REAL_NOT_PAY = [
    # Funding rounds — the single most common dollar amount in the pool (Ashby: 154 of 400
    # rows carry a "$" and NOT ONE of them is pay).
    "We recently raised a $355M Series C https://modal.com/blog/modal-series-c at a $4.65B valuation",
    "Founded in 2014 by CEO Kate Ryder, Maven Clinic has raised more than $425 million from "
    "leading healthcare and technology investors",
    "In February 2026, we raised an additional $100M in Series C financing",
    # Project budgets — the dangerous one: $50K alone IS a plausible salary.
    "Project sizes, complexity and budgets will vary from $50K to $30M+. You will own project "
    "delivery beginning with pre-lease",
    # Company scale.
    "Sezzle currently employs approximately 400 employees and contractors, with gross annual "
    "revenue exceeding $450 million and net income of more than $133 million",
    "Healthcare carries a $1 trillion administrative burden and we're fixing it",
    "Together they've saved over $8 billion and processed over $500 billion",
    # A benefit stipend sitting right after the words "hourly compensation": the cue is
    # there, so only the per-period sanity band ($650/hr is not a wage) rejects it.
    "Why Join BetterHelp New increased hourly compensation. $650 Health Benefit Stipend: "
    "Eligibility for",
    # Indeed serves the occasional nonsense chip. A wide monthly band would turn this into
    # "$1.2M a year" and then reject the job for anyone with a ceiling.
    "$1 - $100,000 a monthFull-timeMinimum of 40 hours per week",
    # A number followed by a time word that is not a period.
    "$120,000 for 5 years of experience",
]


@pytest.mark.parametrize("text,expected", REAL_PAY)
def test_reads_pay_out_of_real_postings(text, expected):
    assert parse_salary(text) == expected


@pytest.mark.parametrize("text", REAL_NOT_PAY)
def test_never_reads_pay_where_the_posting_states_none(text):
    # A false parse is the ONLY way this module can drop a job a user wanted: "unknown"
    # passes, so a job is rejected exclusively on a number we believed.
    assert parse_salary(text) is None


# ── parser rules ──────────────────────────────────────────────────────────────


def test_parses_k_range():
    assert parse_salary("Base pay: $120k - $150k plus equity") == (120_000, 150_000)


def test_parses_comma_range_with_to():
    assert parse_salary("Salary: $95,000 to $115,000 DOE") == (95_000, 115_000)


def test_parses_single_amount():
    assert parse_salary("compensation of $140,000 annually") == (140_000, 140_000)


def test_parses_hourly_to_annual():
    lo, hi = parse_salary("Pay: $65/hr, W2 contract")
    assert lo == hi == 65 * 2080


def test_second_dollar_sign_is_optional():
    assert parse_salary("Salary range: $95,000 - 115,000") == (95_000, 115_000)


def test_swapped_range_is_normalised():
    assert parse_salary("Base salary $150k-$120k") == (120_000, 150_000)


def test_an_amount_with_no_pay_context_is_not_pay():
    # The rule that kills the funding rounds above. A bare range could be anything —
    # budgets, savings, contract value — and reading it as pay rejects a real job.
    assert parse_salary("$150k-$120k") is None
    assert parse_salary("Deals range from $120,000 to $150,000") is None


def test_period_makes_the_amount_pay_without_a_cue():
    assert parse_salary("$52,000 per year") == (52_000, 52_000)


def test_ignores_implausible_amounts():
    assert parse_salary("One-time $500 bonus for referrals") is None
    assert parse_salary("Annual salary: $4,000,000") is None  # above the annual band


def test_no_salary_text():
    assert parse_salary("We offer competitive compensation and benefits") is None
    assert parse_salary("") is None


# ── pass/drop rules ───────────────────────────────────────────────────────────


def _job(desc=""):
    return {"title": "Engineer", "description": desc}


def test_no_bounds_everything_passes():
    assert passes_salary(_job("no pay listed"), None, None, False) is True


def test_unlisted_passes_unless_listed_only():
    # The measured reason (09-26): only ~12% of pool rows carry readable pay, so
    # "unknown rejects" left 6% of the deck and 1 of 124 real applications.
    assert passes_salary(_job("great team"), 100_000, None, False) is True
    assert passes_salary(_job("great team"), 100_000, None, True) is False


def test_below_min_drops():
    assert passes_salary(_job("Base pay: $60k - $80k"), 100_000, None, False) is False


def test_above_max_drops():
    assert passes_salary(_job("Salary: $250,000 - $300,000"), None, 150_000, False) is False


def test_overlap_passes():
    # User 100-140k, job 120-160k → overlaps → pass
    assert passes_salary(_job("Salary: $120k to $160k"), 100_000, 140_000, False) is True


def test_filter_by_salary_batch_and_count():
    profile = {"salary_min": 100_000, "salary_max": None, "salary_listed_only": False}
    jobs = [_job("Base pay: $120k - $140k"), _job("Salary: $50k"), _job("no pay info")]
    kept, dropped = filter_by_salary(jobs, profile)
    assert dropped == 1
    assert len(kept) == 2


def test_filter_noop_without_prefs():
    kept, dropped = filter_by_salary([_job("Salary: $1k")], {})
    assert dropped == 0 and len(kept) == 1
