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

from unittest.mock import patch

from app.routers import jobs as jobs_router


def _row(title, platform="greenhouse", company="c", score=None, status="new", link="https://x/1"):
    return {
        "title": title,
        "company": company,
        "location": "Miami, FL",
        "platform": platform,
        "score": score,
        "status": status,
        "link": link,
        "date_found": "2026-09-01",
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


def test_cap_cuts_the_tail_best_fit_first():
    pool = [
        _row("Event Manager A", score=3, link="https://x/a"),
        _row("Event Manager B", score=9, link="https://x/b"),
        _row("Event Manager C", score=7, link="https://x/c"),
    ]
    out = _queue(pool, ["event manager"], limit=2)

    assert [j["link"] for j in out["jobs"]] == ["https://x/b", "https://x/c"]
    # pool/off_search describe the WHOLE pool, not the capped page.
    assert out["pool"] == 3
    assert out["off_search"] == 0
