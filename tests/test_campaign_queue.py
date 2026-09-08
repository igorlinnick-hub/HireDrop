"""GET /campaign/queue — the Tap work list, decided by the SERVER.

Until now the extension built this from GET /jobs plus browser-local dedup sets, so
nothing outside one Chrome profile could say what was left: no progress anywhere, and a
wiped profile re-applied to jobs already sent. These tests pin the four rules the server
now owns — approved-only, submittable platforms only, nothing already applied (matched by
posting identity, not URL text), and caps — plus the counters that keep a trimmed queue
from looking like a broken one.
"""

from unittest.mock import patch

from app.routers import campaign as campaign_router


class _User:
    id = "u1"
    email = "igor@example.com"


def _row(title, platform="greenhouse", status="approved", link=None, jid="8095311"):
    return {
        "id": f"id-{title}",
        "title": title,
        "company": "c",
        "platform": platform,
        "status": status,
        "link": link or f"https://boards.greenhouse.io/braze/jobs/{jid}?gh_jid={jid}",
        "score": 5,
    }


def _queue(pool, applied_urls=(), done_today=0, budget=30):
    with (
        patch.object(campaign_router.jobs_db, "get_jobs", return_value=pool),
        patch.object(campaign_router.apps_db, "applied_job_urls", return_value=list(applied_urls)),
        patch.object(campaign_router.apps_db, "count_today", return_value=done_today),
        patch.object(campaign_router, "get_tier", return_value="pro"),
        patch.object(campaign_router, "get_submit_mode", return_value="tap"),
        patch.object(campaign_router, "daily_limit", return_value=budget),
    ):
        return campaign_router.campaign_queue(user=_User())


def test_only_approved_swipes_on_submittable_platforms():
    pool = [
        _row("swiped"),
        _row("untouched", status="new", jid="8095312"),
        _row("skipped", status="skipped", jid="8095313"),
        _row("dead swipe", platform="remoteok", jid="8095314"),
    ]
    out = _queue(pool)
    assert [j["title"] for j in out["queue"]] == ["swiped"]


def test_a_job_already_applied_under_another_url_never_returns():
    # The #161 pair: swiped under boards.greenhouse.io, applied under job-boards.
    pool = [_row("Director of Sales")]
    out = _queue(pool, applied_urls=["https://job-boards.greenhouse.io/braze/jobs/8095311"])

    assert out["queue"] == []
    assert out["already_applied"] == 1
    # This is the guard that used to live ONLY in chrome.storage — empty on a fresh
    # profile, which is how the same employer could get a second application.


def test_todays_remaining_budget_trims_the_queue_and_says_so():
    pool = [_row(f"job{i}", jid=f"809531{i}") for i in range(5)]
    out = _queue(pool, done_today=28, budget=30)

    assert out["ready"] == 2  # 30 - 28
    assert out["waiting"] == 5
    assert out["held_by_caps"] == 3  # visible, not silently missing
    assert out["done_today"] == 28


def test_per_platform_ceiling_applies_before_the_daily_budget():
    # Ban safety is not negotiable: one board can't fill the whole run.
    pool = [_row(f"gh{i}", jid=f"80953{i:03d}") for i in range(20)]
    out = _queue(pool, budget=100)

    assert out["ready"] == campaign_router.MAX_PER_PLATFORM
    assert out["cap_per_platform"] == campaign_router.MAX_PER_PLATFORM


def test_an_exhausted_daily_budget_returns_an_empty_queue_not_an_error():
    pool = [_row("swiped")]
    out = _queue(pool, done_today=30, budget=30)

    assert out["queue"] == []
    assert out["waiting"] == 1
    assert out["done_today"] == 30
