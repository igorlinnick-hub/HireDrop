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


def _queue(pool, applied_urls=(), done_today=0, budget=30, mode=None):
    with (
        patch.object(campaign_router.jobs_db, "get_jobs", return_value=pool),
        patch.object(campaign_router.apps_db, "applied_job_urls", return_value=list(applied_urls)),
        patch.object(campaign_router.apps_db, "count_today", return_value=done_today),
        patch.object(campaign_router, "get_tier", return_value="pro"),
        patch.object(campaign_router, "get_submit_mode", return_value="tap"),
        patch.object(campaign_router, "daily_limit", return_value=budget),
    ):
        return campaign_router.campaign_queue(mode=mode, user=_User())


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

    assert out["ready"] == campaign_router.DEFAULT_MAX_PER_PLATFORM
    assert out["cap_per_platform"] == campaign_router.DEFAULT_MAX_PER_PLATFORM


def test_linkedin_runs_under_its_own_tighter_ceiling():
    # LinkedIn's ban rail is 5, not the default 15 — and the tighter number on one platform
    # must not shrink anybody else's slice in the same queue. LinkedIn is not a tap platform
    # yet, so the test widens TAP_APPLY_PLATFORMS to reach the cap loop at all.
    pool = [_row(f"li{i}", platform="linkedin", jid=f"80954{i:03d}") for i in range(9)]
    pool += [_row(f"gh{i}", jid=f"80955{i:03d}") for i in range(20)]
    with patch("app.routers.jobs.TAP_APPLY_PLATFORMS", ("greenhouse", "linkedin")):
        out = _queue(pool, budget=100)

    by: dict[str, int] = {}
    for j in out["queue"]:
        by[j["platform"]] = by.get(j["platform"], 0) + 1
    assert by == {"linkedin": 5, "greenhouse": 15}
    assert out["cap_by_platform"] == {"linkedin": 5}


def test_linkedin_is_not_a_tap_platform_yet():
    # Groundwork only (docs/handoff/linkedin.md): an approved LinkedIn row is not offered.
    out = _queue([_row("li", platform="linkedin", jid="8095499")], budget=100)
    assert out["queue"] == []


def test_an_exhausted_daily_budget_returns_an_empty_queue_not_an_error():
    pool = [_row("swiped")]
    out = _queue(pool, done_today=30, budget=30)

    assert out["queue"] == []
    assert out["waiting"] == 1
    assert out["done_today"] == 30


def test_an_auto_run_is_not_offered_lever_approvals():
    """Lever's submit stops at an hCaptcha a human has to clear, so an AUTO run discards
    those rows — and until 09-26 the server did not know: it spent the day's budget slice on
    them anyway. A user whose approvals are Lever-heavy got a short queue (or none) while the
    dashboard showed the swipes waiting. Same rule, now told to the server.
    """
    pool = [
        _row("lever one", platform="lever", link="https://jobs.lever.co/acme/1", jid="1"),
        _row("lever two", platform="lever", link="https://jobs.lever.co/acme/2", jid="2"),
        _row("greenhouse one", jid="9000001"),
    ]
    out = _queue(pool, budget=2, mode="auto")
    assert [j["title"] for j in out["queue"]] == ["greenhouse one"]
    # And the counters stay honest about what was held back.
    assert out["waiting"] == 1


def test_a_tap_run_still_gets_lever_because_the_human_is_at_the_wheel():
    pool = [_row("lever one", platform="lever", link="https://jobs.lever.co/acme/1", jid="1")]
    assert [j["title"] for j in _queue(pool, mode="tap")["queue"]] == ["lever one"]


def test_an_older_extension_that_sends_no_mode_loses_nothing():
    # Byte-for-byte old behaviour when the parameter is absent or unknown — a build that
    # predates this must never come back with fewer rows than it used to.
    pool = [_row("lever one", platform="lever", link="https://jobs.lever.co/acme/1", jid="1")]
    assert len(_queue(pool)["queue"]) == 1
    assert len(_queue(pool, mode="whatever")["queue"]) == 1
