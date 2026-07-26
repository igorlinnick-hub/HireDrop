"""Salary filter (early gate before AI scoring) — parser + pass/drop rules."""

from modules.salary_filter import filter_by_salary, parse_salary, passes_salary


# ── parser ────────────────────────────────────────────────────────────────────

def test_parses_k_range():
    assert parse_salary("Base pay: $120k - $150k plus equity") == (120_000, 150_000)


def test_parses_comma_range_with_to():
    assert parse_salary("Salary: $95,000 to $115,000 DOE") == (95_000, 115_000)


def test_parses_single_amount():
    assert parse_salary("compensation of $140,000 annually") == (140_000, 140_000)


def test_parses_hourly_to_annual():
    lo, hi = parse_salary("Pay: $65/hr, W2 contract")
    assert lo == hi == 65 * 2080


def test_swapped_range_is_normalised():
    assert parse_salary("$150k-$120k") == (120_000, 150_000)


def test_ignores_implausible_amounts():
    # $500 signing bonus is not a salary; nothing plausible → None
    assert parse_salary("One-time $500 bonus for referrals") is None


def test_no_salary_text():
    assert parse_salary("We offer competitive compensation and benefits") is None
    assert parse_salary("") is None


# ── pass/drop rules ───────────────────────────────────────────────────────────

def _job(desc=""):
    return {"title": "Engineer", "description": desc}


def test_no_bounds_everything_passes():
    assert passes_salary(_job("no pay listed"), None, None, False) is True


def test_unlisted_passes_unless_listed_only():
    assert passes_salary(_job("great team"), 100_000, None, False) is True
    assert passes_salary(_job("great team"), 100_000, None, True) is False


def test_below_min_drops():
    assert passes_salary(_job("$60k - $80k"), 100_000, None, False) is False


def test_above_max_drops():
    assert passes_salary(_job("$250,000 - $300,000"), None, 150_000, False) is False


def test_overlap_passes():
    # User 100-140k, job 120-160k → overlaps → pass
    assert passes_salary(_job("$120k to $160k"), 100_000, 140_000, False) is True


def test_filter_by_salary_batch_and_count():
    profile = {"salary_min": 100_000, "salary_max": None, "salary_listed_only": False}
    jobs = [_job("$120k - $140k"), _job("$50k"), _job("no pay info")]
    kept, dropped = filter_by_salary(jobs, profile)
    assert dropped == 1
    assert len(kept) == 2


def test_filter_noop_without_prefs():
    kept, dropped = filter_by_salary([_job("$1k")], {})
    assert dropped == 0 and len(kept) == 1
