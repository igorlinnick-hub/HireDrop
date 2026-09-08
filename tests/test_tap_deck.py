"""GET /jobs/deck — the Tap deck must show the CURRENT search, not the pool's history.

The pool is INSERT-only and never expires, so reading it whole (GET /jobs) hands the
swipe deck every job ever harvested under every keyword set the user has tried. Measured
on Igor's account 2026-09-08: 535 swipeable rows, 341 of them off-search leftovers from
other days' keywords, oldest six weeks old — a marketing role on top of a deck searched
for "ai engineer". These tests pin the relevance rule, the honesty counters, and the
no-keywords passthrough.
"""

from unittest.mock import patch

from app.routers import jobs as jobs_router


def _row(title, platform="greenhouse", score=None, status="new", link="https://x/1"):
    return {
        "title": title,
        "company": "c",
        "location": "Miami, FL",
        "platform": platform,
        "score": score,
        "status": status,
        "link": link,
        "date_found": "2026-09-01",
    }


class _User:
    id = "u1"


def _deck(pool, keywords):
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=pool),
        patch("app.db.profile.get_profile", return_value={"keywords": keywords}),
    ):
        return jobs_router.get_deck(user=_User())


def test_off_search_leftovers_are_held_back_and_counted():
    pool = [
        _row("AI Engineer", link="https://x/1"),
        _row("Senior Marketing Media Manager", link="https://x/2"),  # the live 09-08 card
    ]
    out = _deck(pool, ["ai engineer", "project manager"])

    assert [c["title"] for c in out["cards"]] == ["AI Engineer"]
    # A deck that silently shrank is indistinguishable from a broken one (#113's lesson).
    assert out["pool"] == 2
    assert out["off_search"] == 1


def test_no_keywords_means_no_filter_exactly_as_at_harvest():
    pool = [_row("Anything At All")]
    assert len(_deck(pool, [])["cards"]) == 1
    assert _deck(pool, [])["off_search"] == 0


def test_only_swipeable_rows_reach_the_deck():
    # Already decided, no link, or a platform nothing can submit to = a dead swipe.
    pool = [
        _row("AI Engineer", status="applied"),
        _row("AI Engineer", link=""),
        _row("AI Engineer", platform="remoteok"),
        _row("AI Engineer", link="https://x/live"),
    ]
    out = _deck(pool, ["ai engineer"])

    assert [c["link"] for c in out["cards"]] == ["https://x/live"]
    # Rows that can never be swiped are not "hidden by your search" — don't count them.
    assert out["pool"] == 1
    assert out["off_search"] == 0


def test_best_fit_first_with_the_fresher_posting_breaking_the_tie():
    # score is a coarse 0-10, so whole bands tie; date is what separates a live posting
    # from a six-week-old one.
    old_tie = {**_row("AI Engineer", score=5, link="https://x/old"), "date_found": "2026-07-29"}
    new_tie = {**_row("AI Engineer", score=5, link="https://x/new"), "date_found": "2026-09-06"}
    best = _row("AI Engineer", score=9, link="https://x/best")
    out = _deck([old_tie, new_tie, best], ["ai engineer"])

    assert [c["link"] for c in out["cards"]] == ["https://x/best", "https://x/new", "https://x/old"]


def test_the_deck_filters_with_the_same_rule_the_harvest_fills_with():
    # One notion of "matches my search" for both sides — drift here is the whole bug.
    from modules.platforms.ats_boards import keyword_match

    pool = [_row("Social Media Coordinator"), _row("Warehouse Picker")]
    out = _deck(pool, ["social media manager"])

    assert [c["title"] for c in out["cards"]] == ["Social Media Coordinator"]
    assert keyword_match("Social Media Coordinator Miami, FL", ["social media manager"]) is True


# --- the swipe write: a lost approve must be loud ------------------------------------


def test_a_swipe_that_changes_nothing_is_a_404_not_a_success():
    # The old endpoint answered {"updated": True} unconditionally. A swipe that wrote
    # nothing then looked identical to one that worked — while the card was already gone
    # from the deck and no run would ever queue that job (Igor 09-08: "до них никогда не
    # доходит очередь"). The deck can only put the card back if the write says it failed.
    from fastapi import HTTPException

    from app.schemas import JobStatusUpdate

    with patch.object(jobs_router.jobs_db, "update_job_status", return_value=0):
        try:
            jobs_router.patch_job_status("missing", JobStatusUpdate(status="approved"), user=_User())
        except HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("a write that changed nothing must not report success")

    with patch.object(jobs_router.jobs_db, "update_job_status", return_value=1):
        out = jobs_router.patch_job_status("j1", JobStatusUpdate(status="approved"), user=_User())
    assert out["updated"] is True


def test_update_job_status_reports_rows_changed():
    from unittest.mock import MagicMock

    from app.db import jobs as jobs_db

    fake = MagicMock()
    chain = fake.table.return_value.update.return_value.eq.return_value.eq.return_value
    chain.execute.return_value.data = [{"id": "j1"}]
    with patch("app.db.jobs.get_supabase", return_value=fake):
        assert jobs_db.update_job_status("u1", "j1", "approved") == 1

    chain.execute.return_value.data = []
    with patch("app.db.jobs.get_supabase", return_value=fake):
        assert jobs_db.update_job_status("u1", "nope", "approved") == 0
