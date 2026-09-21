"""Every role the user named must get a turn at the front.

The walk searches one phrase at a time and always started at index 0, while every cap
counts APPLICATIONS: with six or seven roles the daily budget was spent by the first two
and the tail was never searched at all. Same shape as the ATS-watchlist bug (#154) —
list order became a priority nobody chose.

The server now decides who leads each run and stores the cursor in the campaign row it is
about to overwrite. These tests pin the properties that make that safe when the user edits
their roles between runs.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.routers import campaign as campaign_router
from modules.keyword_rotation import clean_keywords, rotate

ROLES = ["social media manager", "marketing manager", "digital marketing"]


def test_first_run_keeps_the_users_own_order():
    order, nxt = rotate(ROLES, 0)
    assert order == ROLES
    assert nxt == 1


def test_each_run_promotes_the_next_role_to_the_front():
    seen_leads = []
    cursor = 0
    for _ in range(len(ROLES)):
        order, cursor = rotate(ROLES, cursor)
        seen_leads.append(order[0])
    # Everyone leads exactly once per len(keywords) runs — the whole point.
    assert sorted(seen_leads) == sorted(ROLES)


def test_the_set_is_never_changed_only_the_order():
    order, _ = rotate(ROLES, 2)
    assert sorted(order) == sorted(ROLES)
    assert len(order) == len(ROLES)


def test_the_cursor_wraps_instead_of_running_off_the_end():
    # The user deleted two roles since the cursor was stored.
    order, nxt = rotate(["only one"], 7)
    assert order == ["only one"]
    assert nxt == 0


@pytest.mark.parametrize("bad", [None, "", "nope", 3.7, -1])
def test_an_unusable_cursor_never_breaks_a_start(bad):
    order, nxt = rotate(ROLES, bad)
    assert sorted(order) == sorted(ROLES)
    assert 0 <= nxt < len(ROLES)


def test_no_roles_means_no_rotation_and_no_crash():
    assert rotate([], 4) == ([], 0)
    assert rotate(None, 0) == ([], 0)


def test_blank_and_duplicate_roles_are_dropped_before_rotating():
    # A duplicate would otherwise get two turns at the front for the same search.
    assert clean_keywords(["  Marketing Manager ", "marketing manager", "", "  ", "seo"]) == [
        "Marketing Manager",
        "seo",
    ]


def test_start_hands_the_dashboard_the_rotated_order(auth_client):
    """The response — not the browser's local list — is what arms the extension."""
    with (
        patch.object(
            campaign_router.campaign_db,
            "get_state",
            return_value={"filters": {"kw_cursor": 1}},
        ),
        patch.object(
            campaign_router.campaign_db, "start", side_effect=lambda _u, f: {"filters": f}
        ),
        patch.object(
            campaign_router,
            "get_profile",
            return_value={"onboarding_completed": True, "search_radius_miles": None},
        ),
    ):
        body = auth_client.post(
            "/api/v1/campaign/start",
            json={
                "keywords": ROLES,
                "platforms": ["indeed"],
                "location": "Miami",
                "job_type": "full-time",
            },
        ).json()
    assert body["filters"]["keywords"][0] == ROLES[1]
    # And the next run starts one further along.
    assert body["filters"]["kw_cursor"] == 2


def test_start_survives_a_row_with_no_cursor_yet(auth_client):
    with (
        patch.object(campaign_router.campaign_db, "get_state", return_value={"filters": {}}),
        patch.object(
            campaign_router.campaign_db, "start", side_effect=lambda _u, f: {"filters": f}
        ),
        patch.object(
            campaign_router,
            "get_profile",
            return_value={"onboarding_completed": True, "search_radius_miles": None},
        ),
    ):
        body = auth_client.post(
            "/api/v1/campaign/start",
            json={
                "keywords": ROLES,
                "platforms": ["indeed"],
                "location": "Miami",
                "job_type": "full-time",
            },
        ).json()
    assert body["filters"]["keywords"] == ROLES


def _start(auth_client, platforms, mode="auto"):
    with (
        patch.object(campaign_router.campaign_db, "get_state", return_value={"filters": {}}),
        patch.object(
            campaign_router.campaign_db, "start", side_effect=lambda _u, f: {"filters": f}
        ),
        patch.object(campaign_router, "get_submit_mode", return_value=mode),
        patch.object(campaign_router.activity_db, "write", return_value="id"),
        patch.object(
            campaign_router,
            "get_profile",
            return_value={"onboarding_completed": True, "search_radius_miles": None},
        ),
    ):
        return auth_client.post(
            "/api/v1/campaign/start",
            json={
                "keywords": ROLES,
                "platforms": platforms,
                "location": "Miami",
                "job_type": "full-time",
            },
        )


def test_lever_sits_out_an_auto_run_instead_of_blocking_it(auth_client):
    """Lever's captcha needs a human, so an auto run cannot finish one.

    It used to block Start with a modal offering one button — "switch to Tap" — which
    trades the whole auto campaign for one board out of six (Igor, 09-15).
    """
    body = _start(auth_client, ["indeed", "lever", "greenhouse"]).json()
    assert body["started"] is True
    assert "lever" not in body["filters"]["platforms"]
    # Named, not silently dropped: the dashboard and the feed both say what happened.
    assert body["skipped_platforms"] == ["lever"]
    assert body["filters"]["platforms"] == ["indeed", "greenhouse"]


def test_tap_mode_keeps_lever(auth_client):
    body = _start(auth_client, ["indeed", "lever"], mode="tap").json()
    assert "lever" in body["filters"]["platforms"]
    assert body["skipped_platforms"] == []


def test_lever_alone_in_auto_is_refused_out_loud(auth_client):
    # Dropping the only board would start a campaign that can never submit — a silent zero.
    res = _start(auth_client, ["lever"])
    assert res.status_code == 400
    assert res.json()["detail"] == "lever_needs_tap"


def test_a_run_without_lever_is_untouched(auth_client):
    body = _start(auth_client, ["indeed", "ashby"]).json()
    assert body["filters"]["platforms"] == ["indeed", "ashby"]
    assert body["skipped_platforms"] == []


def test_stopping_a_run_keeps_the_cursor_that_says_who_leads_next():
    """The rotation above is only real if the cursor survives the end of a run.

    It didn't: campaign_db.stop() wiped `filters` to {}, and every run ends in a stop, so
    /campaign/start always read cursor 0 and always led with role #1 — which is exactly
    what the 09-19 measurement found (39 of 39 applications on keyword #1). The stop keeps
    kw_cursor and nothing else: the rest of `filters` describes the run that just ended.
    """
    from app.db import campaign as campaign_db

    written: dict = {}
    fake = MagicMock()
    fake.table.return_value.upsert.side_effect = lambda row, **_: written.update(row) or fake
    fake.table.return_value.upsert.return_value = fake
    fake.execute.return_value = MagicMock(data=[])

    with (
        patch.object(
            campaign_db,
            "get_state",
            return_value={"filters": {"kw_cursor": 2, "keywords": ["a", "b", "c"]}},
        ),
        patch.object(campaign_db, "get_supabase", return_value=fake),
    ):
        campaign_db.stop("user-1")

    assert written["running"] is False
    assert written["filters"] == {"kw_cursor": 2}


def test_a_stop_with_no_prior_cursor_is_not_a_crash():
    from app.db import campaign as campaign_db

    written: dict = {}
    fake = MagicMock()
    fake.table.return_value.upsert.side_effect = lambda row, **_: written.update(row) or fake
    fake.execute.return_value = MagicMock(data=[])

    with (
        patch.object(campaign_db, "get_state", return_value={"filters": None}),
        patch.object(campaign_db, "get_supabase", return_value=fake),
    ):
        campaign_db.stop("user-1")

    assert written["filters"] == {"kw_cursor": 0}
