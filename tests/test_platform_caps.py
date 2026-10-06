"""Per-platform ban-safety ceilings (app/db/subscriptions.py MAX_PER_PLATFORM).

Until 10-05 the rail was one number for every platform. LinkedIn restricts accounts for
automation much faster than a job board, so its lane opens at 5/day while everything else
keeps 15. These tests pin the gate the save path enforces and the numbers the extension and
dashboard read — an old reader of the single number must keep getting the default.
"""

from unittest.mock import patch

from app.db import subscriptions


def _check(platform, platform_used, tier="pro"):
    with (
        patch("app.db.subscriptions.user_day_start", return_value=None),
        patch("app.db.subscriptions.get_tier", return_value=tier),
        patch("app.db.subscriptions.get_free_apps_used", return_value=0),
        patch("app.db.subscriptions.get_submit_mode", return_value="auto"),
        patch("app.db.subscriptions.apps_db.count_today", return_value=platform_used),
        patch(
            "app.db.subscriptions.apps_db.count_today_by_platform",
            return_value={platform: platform_used},
        ),
    ):
        return subscriptions.check_can_apply("u1", platform)


def test_linkedin_stops_at_five():
    assert _check("linkedin", 4)["allowed"] is True
    res = _check("linkedin", 5)
    assert res["allowed"] is False
    assert "5 applications per platform" in res["reason"]


def test_other_platforms_keep_fifteen():
    for platform in ("indeed", "ziprecruiter", "greenhouse", "lever", "ashby"):
        assert _check(platform, 5)["allowed"] is True
        assert _check(platform, 14)["allowed"] is True
        res = _check(platform, 15)
        assert res["allowed"] is False
        assert "15 applications per platform" in res["reason"]


def test_usage_summary_serves_the_default_and_the_overrides():
    with (
        patch("app.db.subscriptions.user_day_start", return_value=None),
        patch("app.db.subscriptions.get_tier", return_value="pro"),
        patch("app.db.subscriptions.get_submit_mode", return_value="auto"),
        patch("app.db.subscriptions.apps_db.count_today", return_value=0),
        patch("app.db.subscriptions.apps_db.count_today_by_platform", return_value={}),
    ):
        out = subscriptions.get_usage_summary("u1")
    assert out["max_per_platform"] == 15  # the single number old readers show
    assert out["max_by_platform"] == {"linkedin": 5}


def test_campaign_status_serves_the_default_and_the_overrides():
    from app.routers import campaign as campaign_router

    class _User:
        id = "u1"
        email = "someone@example.com"

    with (
        patch.object(
            campaign_router.campaign_db,
            "get_effective_state",
            return_value={"running": False, "filters": {}, "started_at": None},
        ),
        patch.object(campaign_router, "get_profile", return_value={"platforms": ["indeed"]}),
        patch.object(campaign_router, "remember_zone", return_value=None),
        patch.object(campaign_router.apps_db, "count_today", return_value=0),
        patch.object(campaign_router.apps_db, "count_today_by_platform", return_value={}),
        patch.object(campaign_router.jobs_db, "count_new_jobs", return_value=0),
        patch.object(campaign_router.jobs_db, "count_approved_jobs", return_value=0),
        patch.object(campaign_router, "get_tier", return_value="pro"),
        patch.object(campaign_router, "read_submit_mode", return_value="auto"),
        patch.object(campaign_router, "get_free_apps_used", return_value=0),
    ):
        out = campaign_router.campaign_status(user=_User())
    # The extension in the wild reads this one number into campaignCaps.perPlatform: it must
    # stay the default, or every Indeed run would suddenly stop at 5.
    assert out["limit_per_platform"] == 15
    assert out["limit_by_platform"] == {"linkedin": 5}
