"""Location for the Tap deck — city/state/remote honesty over free text.

Igor's live complaint: a Miami profile was swiping Zoox (Foster City, CA). The pool's
location strings come from 200+ boards in every shape (real samples pinned below). No
coordinates exist in the backend, so the deck answers the only question it can answer
truthfully — same city, same state, or remote — and everything it cannot place PASSES,
like job_type: silence is not a mismatch.
"""

import pytest

from modules.job_location import (
    location_verdict,
    names_foreign_country,
    parse_user_location,
    place_label,
)

MIAMI = parse_user_location("Miami, Florida, US")


def test_parses_the_profile_shape_we_actually_store():
    assert MIAMI == {"city": "miami", "state_code": "fl", "state_name": "florida"}


def test_same_city_and_same_state_fit():
    assert location_verdict("Miami, FL 33134", MIAMI) == "fits"
    assert location_verdict("Tampa, Florida", MIAMI) == "fits"  # state-level: swipe decides
    assert location_verdict("Florida - Remote", MIAMI) == "fits"


def test_the_zoox_card_is_hidden():
    # The card that started this: Lever, Foster City.
    assert location_verdict("Foster City, CA", MIAMI) == "elsewhere"
    assert (
        location_verdict("New York City, NY | Seattle, WA; San Francisco, CA", MIAMI) == "elsewhere"
    )


def test_remote_fits_unless_scoped_to_another_country():
    assert location_verdict("Remote US", MIAMI) == "fits"
    assert location_verdict("Remote", MIAMI) == "fits"
    assert location_verdict("Americas Remote", MIAMI) == "fits"
    # Legal-entity scopes from the live pool: not workable from Florida.
    assert location_verdict("Remote (Bulgaria)", MIAMI) == "elsewhere"
    assert location_verdict("Remote - Ontario, Canada", MIAMI) == "elsewhere"
    # Multi-country scope that includes nothing US-ish still reads elsewhere…
    assert location_verdict("Remote (Bulgaria, Ireland, United Kingdom)", MIAMI) == "elsewhere"


def test_foreign_on_site_is_elsewhere():
    for loc in (
        "Sweden",
        "Singapore",
        "Tel Aviv, Israel",
        "India - Bangalore",
        "Mexico City, Mexico",
    ):
        assert location_verdict(loc, MIAMI) == "elsewhere", loc


def test_silence_and_bare_mode_words_pass():
    # The legacy pool must not vanish: empty and unplaceable strings are unknown → shown.
    assert location_verdict("", MIAMI) == "unknown"
    assert location_verdict(None, MIAMI) == "unknown"
    assert location_verdict("Hybrid", MIAMI) == "unknown"


def test_profile_without_a_city_matches_at_state_level():
    fl_only = parse_user_location("Florida, US")
    assert fl_only["city"] is None
    assert location_verdict("Orlando, FL", fl_only) == "fits"
    assert location_verdict("Austin, TX", fl_only) == "elsewhere"


def test_remote_scoped_to_a_bare_foreign_city_is_elsewhere():
    # The 09-21 India leak: boards write the hub city WITHOUT the country, and the
    # remote branch only knew countries — "Remote - Bengaluru" sailed through.
    assert location_verdict("Remote - Bengaluru", MIAMI) == "elsewhere"
    assert location_verdict("Remote (Pune)", MIAMI) == "elsewhere"
    assert location_verdict("Toronto - Remote", MIAMI) == "elsewhere"
    # …but a US town sharing a foreign name is protected by its "City, ST" shape.
    assert location_verdict("Remote - Melbourne, FL", MIAMI) == "fits"


def test_names_foreign_country_is_the_country_level_gate():
    # Fires: plain country, scoped remote, bare foreign hub city.
    for loc in ("India - Bangalore", "Remote (Bulgaria)", "Bengaluru", "Hyderabad, Telangana"):
        assert names_foreign_country(loc), loc
    # Never fires: US shapes, US-remote, foreign-named US towns, silence.
    for loc in (
        "Remote",
        "Remote US",
        "Miami, FL",
        "Dublin, OH",
        "Athens, GA",
        "Albuquerque, New Mexico",
        "Hybrid",
        "",
        None,
    ):
        assert not names_foreign_country(loc), loc


def test_coarse_remote_profile_still_hides_foreign_rows_in_the_deck():
    # "remote"/"usa" parse to no city/state, which used to switch the location filter
    # OFF — the exact hole that put India in a "remote" search (09-21).
    from unittest.mock import patch

    from app.routers import jobs as jobs_router

    class _User:
        id = "u1"

    def _row(title, loc):
        return {
            "title": title,
            "location": loc,
            "platform": "greenhouse",
            "status": "new",
            "link": f"https://boards.greenhouse.io/x/jobs/{abs(hash(title))}",
            "job_type": None,
        }

    rows = [
        _row("AI Engineer", "Bengaluru, India"),
        _row("AI Engineer 2", "Remote - Bengaluru"),
        _row("AI Engineer 3", "Remote"),
    ]
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=rows),
        patch(
            "app.db.profile.get_profile",
            return_value={"keywords": ["ai engineer"], "location": "remote"},
        ),
    ):
        out = jobs_router.get_deck(user=_User())
    assert [c["title"] for c in out["cards"]] == ["AI Engineer 3"]


def test_country_gate_applies_to_every_profile_us_only():
    # 09-27, Igor: US-only product. A leftover "europe" location no longer opts out.
    from app.routers.jobs import on_search_filter

    rows = [
        {"title": "AI Engineer", "location": "Berlin, Germany", "job_type": None},
    ]
    profile = {"keywords": ["ai engineer"], "location": "europe", "job_type": ""}
    assert on_search_filter(rows, profile) == []


def test_profile_without_a_location_disables_the_filter_entirely():
    # Like keywords: nothing to compare against must mean "show", not "hide everything".
    # The first full-suite run caught exactly this — every deck test emptied out because
    # its mock profile carries no location.
    from unittest.mock import patch

    from app.routers import jobs as jobs_router

    class _User:
        id = "u1"

    row = {
        "title": "AI Engineer",
        "location": "Foster City, CA",
        "platform": "greenhouse",
        "status": "new",
        "link": "https://boards.greenhouse.io/x/jobs/1234567",
        "job_type": None,
    }
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=[row]),
        patch("app.db.profile.get_profile", return_value={"keywords": ["ai engineer"]}),
    ):
        out = jobs_router.get_deck(user=_User())
    assert [c["title"] for c in out["cards"]] == ["AI Engineer"]


def test_foreign_remote_does_not_pass_as_a_fit():
    """ "Chile, Remote" scored as a fit for a Honolulu candidate and, being the freshest
    row, would have been the one the night shift applied to (live 09-26).

    The remote branch trusts _NON_US_RE: a country missing from that list turns into
    "remote, therefore anywhere" for a US-only user — the same hole the India leak came
    through in September. These are the additions, checked in both directions.
    """
    honolulu = parse_user_location("Honolulu, Hawaii, US")
    for text in (
        "Chile, Remote",
        "Remote - Chile",
        "Remote - Peru",
        "Remote, Costa Rica",
        "Dubai, UAE",
        "Remote - Taiwan",
        "Remote - Croatia",
        "Remote - Czechia",
    ):
        assert names_foreign_country(text), text
        assert location_verdict(text, honolulu) == "elsewhere", text


def test_us_towns_named_after_countries_are_still_american():
    """The price of a longer country list is false positives on US place names, so the
    state-hint guard carries more weight now: Peru IN, Panama City FL and Santiago CA
    are American towns and must never read as foreign."""
    for text in (
        "Peru, IN",
        "Panama City, FL",
        "Santiago, CA",
        "Lebanon, PA",
        "Cuba, MO",
    ):
        assert not names_foreign_country(text), text
    assert location_verdict("Peru, IN", parse_user_location("Peru, Indiana, US")) == "fits"


# ── work setting (09-27) ──────────────────────────────────────────────────────────
from modules.job_location import matches_work_setting, posting_work_setting  # noqa: E402


@pytest.mark.parametrize(
    "loc,title,expected",
    [
        ("Remote - US", "", "remote"),
        ("Hybrid - San Francisco, California", "", "hybrid"),
        ("Remote or Hybrid - New York, NY", "", "remote"),
        ("Honolulu, HI", "", "onsite"),
        ("New York, NY", "Account Executive (Remote)", "remote"),
        ("United States", "", None),
        ("USA", "", None),
        ("", "", None),
    ],
)
def test_posting_work_setting(loc, title, expected):
    assert posting_work_setting(loc, title) == expected


def test_remote_only_user_rejects_office_role_in_own_city():
    # The hole this closes: geography says "fits", the arrangement does not.
    assert not matches_work_setting("Honolulu, HI", "Marketing Manager", "remote")
    assert not matches_work_setting("Hybrid - Honolulu, HI", "Marketing Manager", "remote")


def test_remote_only_user_keeps_remote_and_unknown():
    assert matches_work_setting("Remote - US", "Marketing Manager", "remote")
    assert matches_work_setting("United States", "Marketing Manager", "remote")
    assert matches_work_setting("", "Marketing Manager", "remote")


@pytest.mark.parametrize("wanted", ["", None, "any", "hybrid", "onsite"])
def test_other_settings_never_narrow_here(wanted):
    # Place is location_verdict's job; a remote posting fits everyone.
    for loc in ("Honolulu, HI", "Remote - US", "Hybrid - Honolulu, HI", ""):
        assert matches_work_setting(loc, "Marketing Manager", wanted)


def test_on_search_filter_applies_remote_only_setting():
    from app.routers.jobs import on_search_filter

    rows = [
        {"title": "Marketing Manager", "location": "Honolulu, HI"},
        {"title": "Marketing Manager", "location": "Remote - US"},
    ]
    base = {"keywords": ["marketing manager"], "location": "Honolulu, Hawaii, US"}
    assert len(on_search_filter(rows, base)) == 2
    kept = on_search_filter(rows, {**base, "work_setting": "remote"})
    assert [r["location"] for r in kept] == ["Remote - US"]


def test_a_foreign_region_is_abroad_and_americas_is_not():
    """ "Remote - EMEA" read as plain remote — a fit — for a US-only user (10-01)."""
    from modules.job_location import (
        location_verdict,
        names_foreign_country,
        names_non_us_place,
        parse_user_location,
    )

    user = parse_user_location("Houston, Texas, US")
    for row in ("Remote - EMEA", "Remote (Europe)", "Remote - LATAM", "Remote - APAC", "EU"):
        assert names_foreign_country(row), row
        assert location_verdict(row, user) == "elsewhere", row
    for row in ("Remote", "Remote - United States", "Remote, Americas", "Remote - US or EMEA"):
        assert not names_foreign_country(row), row
        assert location_verdict(row, user) == "fits", row
    # A job TITLE can carry the region the location left out.
    assert names_foreign_country("Account Executive, EMEA")
    assert not names_foreign_country("Account Executive, Americas")
    # An airport code is not Southeast Asia, and Georgia is a state.
    assert not names_foreign_country("Seattle (SEA)")
    assert not names_foreign_country("Field Rep - Georgia")
    # The raw fact, for questions: a foreign place is named even when the US is too.
    assert names_non_us_place("authorized to work in Canada? US applicants see above")
    assert not names_non_us_place("authorized to work in the United States")


# ── place_label: History's "where was this job" ────────────────────────────────────
# Every left-hand string is a real location from a live account's applications (10-08).


@pytest.mark.parametrize(
    "raw, place",
    [
        ("Houston, TX 77008", "Houston, TX"),
        ("Missouri City, TX 77489", "Missouri City, TX"),
        ("New York, NY (HQ)", "New York, NY"),
        ("Hybrid work in Houston, TX 77056", "Houston, TX"),
        ("Houston, TX (University Place area)", "Houston, TX"),
        ("6399 Highway 6 South, Houston, TX 77083", "Houston, TX"),
        ("Addison, TX (Hybrid); Bellevue, WA (Hybrid)", "Addison, TX"),
        ("Remote - Boston, Massachusetts; Remote - Chicago, IL", "Boston, MA"),
        ("Hybrid - San Francisco, California", "San Francisco, CA"),
        ("Kansas City, Missouri", "Kansas City, MO"),
        ("St. Louis, MO", "St. Louis, MO"),
        ("houston, TX", "Houston, TX"),
    ],
)
def test_place_label_reduces_board_text_to_city_and_state(raw, place):
    assert place_label(raw) == place


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Remote, US",
        "United States (Remote)",
        "Hybrid",
        "Austin",  # no state: a guess, not a place
        "Paris, France",
        # A state NAME must end its segment, or this reads as a city in New York state.
        "San Francisco, New York or Remote (USA)",
        # Codes are upper-case only: "in" here is a word, not Indiana.
        "Remote, in office",
    ],
)
def test_place_label_says_nothing_rather_than_guess(raw):
    assert place_label(raw) is None
