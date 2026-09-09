"""Auto vs Tap must never be GUESSED.

Both ends used to answer "which mode?" from nothing: the backend's profile read swallowed
every exception and returned "auto", and the extension read a request that might not have
arrived. Either guess sends a Tap user a full auto walk over cards they never swiped —
applications that cannot be taken back. The read now says "I don't know" out loud, and
/campaign/status carries that through so the extension can refuse to start.

The cap side keeps the opposite default on purpose: tap's daily limit is the higher one,
so an unknown mode must NOT raise a ban-safety rail.
"""

from unittest.mock import patch

from app.db import subscriptions
from app.routers import campaign as campaign_router


def _profile_read_raises():
    return patch.object(
        subscriptions, "get_supabase", side_effect=RuntimeError("supabase unreachable")
    )


def test_unreadable_profile_reads_as_unknown_not_auto():
    with _profile_read_raises():
        assert subscriptions.read_submit_mode("u1") is None


def test_missing_row_is_the_default_not_an_unknown():
    # No profile row = the user never chose = "auto". That IS an answer, unlike an error.
    with patch.object(subscriptions, "get_supabase") as gs:
        gs.return_value.table.return_value.select.return_value.eq.return_value.execute.return_value.data = []
        assert subscriptions.read_submit_mode("u1") == "auto"


def test_stored_tap_survives_the_round_trip():
    with patch.object(subscriptions, "get_supabase") as gs:
        gs.return_value.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
            {"submit_mode": "TAP"}
        ]
        assert subscriptions.read_submit_mode("u1") == "tap"


def test_cap_side_still_falls_back_to_the_lower_ceiling():
    # get_submit_mode is what daily_limit() consumes: on an unreadable profile it must
    # stay "auto", because tap's cap is several times higher.
    with _profile_read_raises():
        assert subscriptions.get_submit_mode("u1") == "auto"


def test_status_tells_the_extension_the_mode_was_unreadable(auth_client):
    with (
        patch.object(campaign_router, "read_submit_mode", return_value=None),
        patch.object(campaign_router, "get_tier", return_value="pro"),
    ):
        body = auth_client.get("/api/v1/campaign/status").json()
    assert body["submit_mode_known"] is False
    # The reported value stays the conservative one so cap math downstream is unchanged.
    assert body["submit_mode"] == "auto"


def test_status_marks_a_real_answer_as_known(auth_client):
    with (
        patch.object(campaign_router, "read_submit_mode", return_value="tap"),
        patch.object(campaign_router, "get_tier", return_value="pro"),
    ):
        body = auth_client.get("/api/v1/campaign/status").json()
    assert body["submit_mode_known"] is True
    assert body["submit_mode"] == "tap"
