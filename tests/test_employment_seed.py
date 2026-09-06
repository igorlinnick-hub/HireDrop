"""Seeding current_employer / current_title from the parsed resume.

The rule that matters: seeding only ever fills a BLANK field. What the user typed in
Settings must survive every resume regeneration — a filler that overwrites a person's
correction with a stale resume line would put wrong data on a real application.
"""

from unittest.mock import patch

from app.db import profile as profile_db
from app.routers.profile import _seed_employment_from_resume


def _seed(existing: dict, employer: str, title: str) -> dict:
    """Run fill_current_employment_if_blank against a fake stored profile,
    returning the payload it would have written to Supabase."""
    written = {}

    class _Chain:
        def update(self, payload):
            written.update(payload)
            return self

        def eq(self, *_a, **_k):
            return self

        def execute(self):
            return None

    class _Client:
        def table(self, _name):
            return _Chain()

    with (
        patch.object(profile_db, "get_profile", return_value=existing),
        patch.object(profile_db, "get_supabase", return_value=_Client()),
    ):
        profile_db.fill_current_employment_if_blank("u1", employer, title)
    return written


def test_seeds_both_fields_when_profile_is_blank():
    assert _seed({"current_employer": "", "current_title": ""}, "Acme Corp", "Engineer") == {
        "current_employer": "Acme Corp",
        "current_title": "Engineer",
    }


def test_never_overwrites_what_the_user_typed():
    written = _seed(
        {"current_employer": "User's Own Co", "current_title": "Staff Engineer"},
        "Stale Resume Corp",
        "Junior Dev",
    )
    assert written == {}


def test_fills_only_the_blank_half():
    written = _seed({"current_employer": "User's Own Co", "current_title": ""}, "Other", "Engineer")
    assert written == {"current_title": "Engineer"}


def test_blank_and_whitespace_resume_values_write_nothing():
    assert _seed({"current_employer": "", "current_title": ""}, "", "   ") == {}


def test_router_seed_reads_experience_zero():
    data = {
        "experience": [
            {"company": "Most Recent Inc", "title": "Senior Engineer"},
            {"company": "Older Co", "title": "Junior"},
        ]
    }
    with patch.object(profile_db, "fill_current_employment_if_blank", return_value={}) as fill:
        _seed_employment_from_resume("u1", data)
    fill.assert_called_once_with("u1", "Most Recent Inc", "Senior Engineer")


def test_router_seed_survives_a_resume_with_no_experience():
    """A profile write must never fail the resume build that triggered it."""
    with patch.object(profile_db, "fill_current_employment_if_blank", return_value={}) as fill:
        _seed_employment_from_resume("u1", {})
    fill.assert_called_once_with("u1", "", "")


def test_router_seed_swallows_db_errors():
    with patch.object(profile_db, "fill_current_employment_if_blank", side_effect=RuntimeError):
        _seed_employment_from_resume("u1", {"experience": [{"company": "X", "title": "Y"}]})


# ── address seeding (same paid parse, same only-fill-blank contract) ─────────


def _seed_addr(existing: dict, city: str, state: str, postal: str) -> dict:
    written = {}

    class _Chain:
        def update(self, payload):
            written.update(payload)
            return self

        def eq(self, *_a, **_k):
            return self

        def execute(self):
            return None

    class _Client:
        def table(self, _name):
            return _Chain()

    with (
        patch.object(profile_db, "get_profile", return_value=existing),
        patch.object(profile_db, "get_supabase", return_value=_Client()),
    ):
        profile_db.fill_address_if_blank("u1", city, state, postal)
    return written


def test_address_seeds_blank_fields_only():
    written = _seed_addr({"city": "", "state": "", "postal_code": ""}, "Miami", "FL", "33101")
    assert written == {"city": "Miami", "state": "FL", "postal_code": "33101"}


def test_address_never_overwrites_user_input():
    written = _seed_addr(
        {"city": "Tampa", "state": "FL", "postal_code": "33601"}, "Miami", "FL", "33101"
    )
    assert written == {}


def test_split_location_variants():
    from app.routers.profile import _split_location

    assert _split_location("Miami, FL 33101") == ("Miami", "FL", "33101")
    assert _split_location("Miami, Florida, USA") == ("Miami", "Florida", "")
    assert _split_location("Miami FL") == ("Miami", "FL", "")
    assert _split_location("Remote") == ("", "", "")
    assert _split_location("") == ("", "", "")
    # ZIP+4 collapses to the 5-digit form the profile stores.
    assert _split_location("Brooklyn, NY 11201-1234") == ("Brooklyn", "NY", "11201")
