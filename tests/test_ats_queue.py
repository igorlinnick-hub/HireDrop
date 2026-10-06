"""GET /jobs/ats-queue — the AUTO campaign must apply to the current search, not the archive.

The Tap deck learned this on 09-08 (test_tap_deck.py); the auto ATS walk did not, and on
09-13 it cost a whole run. Live evidence from Igor's account: profile keywords read
`event manager` / `sales & marketing`, the pool held 321 greenhouse rows, and the campaign
built its queue from a raw `GET /jobs` — walking 15 Oura/Braze ENGINEERING postings
harvested weeks earlier under `ai engineer`. The judge scored them 2-12 against a bar of
35, skipped every one honestly, and the run reported "ATS pool complete — Campaign
finished" after 2m26s with zero applications. Every skip was individually correct, which
is exactly why three days of reports read "fit skips are honest" and nobody looked at the
INPUT.

These tests pin the rule that the queue and the deck are cut the same way.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app.routers import jobs as jobs_router


def _days_ago(n: int) -> str:
    """Relative on purpose — the queue drops postings older than MAX_POOL_AGE_DAYS, so a
    hard-coded date turns this suite red the day it ages past the cap."""
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


def _row(title, platform="greenhouse", company="c", score=None, status="new", link="https://x/1"):
    return {
        "title": title,
        "company": company,
        "location": "Miami, FL",
        "platform": platform,
        "score": score,
        "status": status,
        "link": link,
        "date_found": _days_ago(2),
    }


class _User:
    id = "u1"


def _queue(pool, keywords, platform="greenhouse", limit=20):
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=pool),
        patch("app.db.profile.get_profile", return_value={"keywords": keywords}),
    ):
        return jobs_router.get_ats_queue(platform=platform, limit=limit, user=_User())


def test_the_0913_run_rebuilt_the_archive_no_longer_reaches_the_queue():
    """The exact shape of the live failure: stale engineering rows, an event-manager profile."""
    pool = [
        _row("Senior iOS Engineer, Connectivity", company="Oura"),
        _row("Senior MLOps Engineer", company="Oura"),
        _row("Senior Data Engineer, Product", company="Oura"),
        _row("Event Manager", company="Braze", link="https://x/live"),
    ]
    out = _queue(pool, ["event manager", "sales & marketing"])

    assert [j["link"] for j in out["jobs"]] == ["https://x/live"]
    # Say what was held back — a queue that silently shrank looks like a broken one (#113).
    assert out["pool"] == 4
    assert out["off_search"] == 3


def test_no_keywords_means_no_filter_exactly_as_at_harvest():
    pool = [_row("Anything At All")]
    assert len(_queue(pool, [])["jobs"]) == 1
    assert _queue(pool, [])["off_search"] == 0


def test_queue_matches_the_deck_rule_on_the_same_pool():
    """One rule, one place: what we show to swipe and what we auto-apply to cannot drift."""
    pool = [
        _row("Event Manager", link="https://x/1"),
        _row("Senior MLOps Engineer", link="https://x/2"),
    ]
    keywords = ["event manager"]
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=pool),
        patch("app.db.profile.get_profile", return_value={"keywords": keywords}),
    ):
        deck = jobs_router.get_deck(user=_User())
        queue = jobs_router.get_ats_queue(platform="greenhouse", limit=20, user=_User())

    assert [c["link"] for c in deck["cards"]] == [j["link"] for j in queue["jobs"]]


def test_only_applyable_rows_reach_the_queue():
    # Already decided, no link, or another platform = nothing this walk can submit.
    pool = [
        _row("Event Manager", status="applied"),
        _row("Event Manager", link=""),
        _row("Event Manager", platform="ashby"),
        _row("Event Manager", link="https://x/live"),
    ]
    assert [j["link"] for j in _queue(pool, ["event manager"])["jobs"]] == ["https://x/live"]


def test_cap_cuts_the_tail_freshest_first():
    # The pool scorer's 0-10 no longer orders the queue (Igor, 09-30) — freshness does.
    # Three employers: one per company (fit_queue.COMPANY_CAP) is not what this pins.
    pool = [
        {
            **_row("Event Manager A", score=3, link="https://x/a"),
            "company": "Co A",
            "date_found": _days_ago(0),
        },
        {
            **_row("Event Manager B", score=9, link="https://x/b"),
            "company": "Co B",
            "date_found": _days_ago(3),
        },
        {
            **_row("Event Manager C", score=7, link="https://x/c"),
            "company": "Co C",
            "date_found": _days_ago(1),
        },
    ]
    out = _queue(pool, ["event manager"], limit=2)

    assert [j["link"] for j in out["jobs"]] == ["https://x/a", "https://x/c"]
    # pool/off_search describe the WHOLE pool, not the capped page.
    assert out["pool"] == 3
    assert out["off_search"] == 0


def test_the_queue_does_not_open_a_posting_that_has_aged_out():
    """Same cap as the deck (MAX_POOL_AGE_DAYS), same reason: `date_found` only breaks a
    tie between EQUAL scores, so a stale row with a good score sat at the head of the walk
    and spent a page load on a posting that closed weeks ago. Measured 09-22 — 91% of real
    applications went to postings harvested inside 7 days."""
    stale = {
        **_row("Event Manager", score=9, link="https://x/stale"),
        "date_found": _days_ago(jobs_router.MAX_POOL_AGE_DAYS + 1),
    }
    live = {**_row("Event Manager", score=3, link="https://x/live")}
    out = _queue([stale, live], ["event manager"])

    assert [j["link"] for j in out["jobs"]] == ["https://x/live"]
    assert out["stale"] == 1
    assert out["off_search"] == 0


def test_a_posting_waiting_on_the_person_is_not_walked_again():
    """10-02: DoorDash 8237299 sat in an open hand-back (9 required fields empty) and the
    queue served it five times in one night — every run filled it to the same wall. Hand-back
    rows from the extension carry no job_id, so the match is by the posting's identity."""
    pool = [
        _row("Event Manager", link="https://job-boards.greenhouse.io/doordashusa/jobs/8237299"),
        _row("Event Manager", link="https://job-boards.greenhouse.io/tia/jobs/8005735003"),
    ]
    waiting = ["https://job-boards.greenhouse.io/doordashusa/jobs/8237299?gh_src=abc"]
    with patch("app.db.handbacks.open_urls", return_value=waiting) as reader:
        out = _queue(pool, ["event manager"])
    assert [j["link"] for j in out["jobs"]] == [
        "https://job-boards.greenhouse.io/tia/jobs/8005735003"
    ]
    # Only rows nobody answered yet: an answered hand-back is meant to run again.
    assert reader.call_args.kwargs.get("waiting_only") is True


def test_an_unreadable_handback_table_does_not_empty_the_queue():
    pool = [_row("Event Manager", link="https://x/1")]
    with patch("app.db.handbacks.open_urls", side_effect=RuntimeError("down")):
        out = _queue(pool, ["event manager"])
    assert len(out["jobs"]) == 1
