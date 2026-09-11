"""Location for the Tap deck — city/state/remote honesty over free text.

Igor's live complaint: a Miami profile was swiping Zoox (Foster City, CA). The pool's
location strings come from 200+ boards in every shape (real samples pinned below). No
coordinates exist in the backend, so the deck answers the only question it can answer
truthfully — same city, same state, or remote — and everything it cannot place PASSES,
like job_type: silence is not a mismatch.
"""

from modules.job_location import location_verdict, parse_user_location

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
