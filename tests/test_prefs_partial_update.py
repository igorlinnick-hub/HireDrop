"""POST /profile/prefs changes only the fields the request carries.

The dashboard saves a filter (a keyword, the location, a chip) without restating
every other preference; whatever it leaves out has to stay as saved.
"""

from unittest.mock import patch

API = "/api/v1"

SAVED = {
    "name": "Ana",
    "last_name": "Diaz",
    "phone": "+1 555 0100",
    "writing_style": "plain",
    "keywords": ["nurse"],
    "location": "Miami, FL",
    "job_type": "part-time",
    "platforms": ["indeed", "ziprecruiter", "greenhouse", "lever", "ashby"],
    "work_setting": "hybrid",
}


def _written(auth_client, body, saved=SAVED):
    with (
        patch("app.routers.profile.profile_db.get_profile", return_value=dict(saved)),
        patch("app.routers.profile.profile_db.update_profile", return_value={}) as update,
    ):
        res = auth_client.post(f"{API}/profile/prefs", json=body)
    assert res.status_code == 200
    return update.call_args[0][1]


def test_a_filter_write_without_platforms_keeps_all_five(auth_client):
    payload = _written(auth_client, {"keywords": ["rn"], "location": "remote"})
    assert payload["platforms"] == SAVED["platforms"]
    assert payload["keywords"] == ["rn"]
    assert payload["location"] == "remote"


def test_every_left_out_field_keeps_its_saved_value(auth_client):
    payload = _written(auth_client, {})
    for k in ("name", "last_name", "phone", "writing_style", "keywords", "location", "job_type"):
        assert payload[k] == SAVED[k], k
    assert payload["platforms"] == SAVED["platforms"]
    assert "work_setting" not in payload  # update_profile leaves an absent one alone


def test_null_means_left_out(auth_client):
    payload = _written(auth_client, {"platforms": None, "job_type": None})
    assert payload["platforms"] == SAVED["platforms"]
    assert payload["job_type"] == SAVED["job_type"]


def test_sent_fields_replace_the_saved_ones(auth_client):
    payload = _written(
        auth_client,
        {"platforms": ["indeed"], "job_type": "", "work_setting": "onsite"},
    )
    assert payload["platforms"] == ["indeed"]
    assert payload["job_type"] == ""
    assert payload["work_setting"] == "onsite"


def test_a_new_profile_gets_update_profiles_defaults(auth_client):
    # Nothing saved and nothing sent: the columns are left to update_profile, which
    # fills the same defaults the request model used to.
    payload = _written(auth_client, {}, saved={})
    assert payload == {}
