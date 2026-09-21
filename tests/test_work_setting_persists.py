"""A filter the user picked has to still be there after Stop.

Work setting (remote/hybrid/onsite) lived in React state and travelled only in the
START payload. Stop calls router.refresh(), the dashboard re-reads the profile, and the
chip came back "Any setting" — so the user re-picked it every single run (Igor,
2026-09-21). It has a column now, and the rule that matters is the partial-save one:
a caller that says nothing about work_setting must not erase it.
"""

from unittest.mock import patch

API = "/api/v1"

BASE_PREFS = {
    "keywords": ["rn"],
    "location": "remote",
    "job_type": "part-time",
    "platforms": ["indeed"],
}


def _saved_payload(auth_client, body):
    with (
        patch("app.routers.profile.profile_db.get_profile", return_value={}),
        patch("app.routers.profile.profile_db.update_profile", return_value={}) as update,
    ):
        res = auth_client.post(f"{API}/profile/prefs", json=body)
    assert res.status_code == 200
    return update.call_args[0][1]


def test_a_picked_setting_is_written(auth_client):
    payload = _saved_payload(auth_client, {**BASE_PREFS, "work_setting": "hybrid"})
    assert payload["work_setting"] == "hybrid"


def test_any_is_a_real_answer_not_a_missing_one(auth_client):
    # "" means "don't filter" — it has to be stored, or the next load can't tell it
    # apart from never having been set and the chip drifts back on its own.
    payload = _saved_payload(auth_client, {**BASE_PREFS, "work_setting": ""})
    assert payload["work_setting"] == ""


def test_a_caller_that_omits_it_does_not_wipe_it(auth_client):
    # Anything posting prefs without the field (an older tab, the onboarding wizard)
    # must leave the saved value alone rather than blanking it.
    payload = _saved_payload(auth_client, BASE_PREFS)
    assert "work_setting" not in payload


def test_the_other_prefs_still_save_alongside_it(auth_client):
    payload = _saved_payload(auth_client, {**BASE_PREFS, "work_setting": "onsite"})
    assert payload["job_type"] == "part-time"
    assert payload["keywords"] == ["rn"]
    assert payload["location"] == "remote"
    assert payload["platforms"] == ["indeed"]


def test_update_profile_only_writes_it_when_present():
    """The db helper is the second half of the same rule."""
    from app.db import profile as profile_db

    with (
        patch("app.db.profile.get_supabase") as sb,
        patch("app.db.profile.get_profile", return_value={}),
    ):
        profile_db.update_profile("u1", {**BASE_PREFS, "work_setting": "remote"})
        with_field = sb().table().update.call_args[0][0]

        sb.reset_mock()
        profile_db.update_profile("u1", dict(BASE_PREFS))
        without_field = sb().table().update.call_args[0][0]

    assert with_field["work_setting"] == "remote"
    assert "work_setting" not in without_field
