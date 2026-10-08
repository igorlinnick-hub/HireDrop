"""POST /profile/prefs changes only the fields the request carries.

The dashboard saves a filter (a keyword, the location, a chip) without restating
every other preference; whatever it leaves out has to stay exactly as saved,
including "" (Any), which a read-then-rewrite would turn into a default.
"""

from unittest.mock import patch

API = "/api/v1"


def _written(auth_client, body):
    """The column update /profile/prefs sends to Supabase, or None if it sends none."""
    with (
        patch("app.db.profile.get_supabase") as sb,
        patch("app.db.profile.get_profile", return_value={}),
    ):
        res = auth_client.post(f"{API}/profile/prefs", json=body)
    assert res.status_code == 200
    update = sb().table().update
    return update.call_args[0][0] if update.called else None


def test_a_filter_write_without_platforms_leaves_platforms_alone(auth_client):
    assert _written(auth_client, {"keywords": ["rn"], "location": "remote"}) == {
        "keywords": ["rn"],
        "location": "remote",
    }


def test_an_empty_request_writes_nothing(auth_client):
    assert _written(auth_client, {}) is None


def test_null_means_left_out(auth_client):
    body = {"keywords": ["rn"], "platforms": None, "job_type": None}
    assert _written(auth_client, body) == {"keywords": ["rn"]}


def test_sent_fields_are_written_as_sent(auth_client):
    body = {"platforms": ["indeed"], "job_type": "", "work_setting": "onsite"}
    assert _written(auth_client, body) == body


def test_only_search_fields_reach_the_profile(auth_client):
    body = {"keywords": ["rn"], "name": "Someone else", "resume_url": "x.pdf"}
    assert _written(auth_client, body) == {"keywords": ["rn"]}
