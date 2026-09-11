"""GET /tools/run-report — the funnel and the yield, not liveness.

The bug that made this necessary: 2026-09-08, an auto walk ran 20 minutes, opened dozens of
postings and submitted ZERO — every one skipped on fit — while every health signal stayed
green. Liveness was the wrong thing to measure. These tests pin the verdict for each way a
run can look busy and produce nothing.
"""

from unittest.mock import patch

from app.db import activity as activity_db


def _counts(by_type, total=None, auth_401=0):
    return {
        "since": None,
        "total": total if total is not None else sum(by_type.values()),
        "by_type": by_type,
        "auth_401": auth_401,
        "skipped_fit": by_type.get("skipped_fit", 0),
        "skipped_no_resume": by_type.get("skipped_no_resume", 0),
        "resume_fail": 0,
    }


def _report(by_type, minutes=20, total=None, auth_401=0):
    with (
        patch.object(activity_db, "summary", return_value=_counts(by_type, total, auth_401)),
        patch.object(activity_db, "_minutes_spanned", return_value=minutes),
    ):
        return activity_db.run_report("u1")


def test_busy_and_producing_nothing_is_named_outright():
    # The 09-08 shape. This is the sentence that was missing for 20 minutes.
    out = _report({"opened": 43, "skipped_fit": 41}, minutes=20)

    assert out["applied"] == 0
    assert out["opened"] == 43
    assert "applied to NONE" in out["verdict"]
    assert "fit gate" in out["verdict"]


def test_it_names_the_dominant_loss_not_just_the_total():
    out = _report({"opened": 30, "dead_link": 22, "skipped_fit": 3}, minutes=15)
    assert "dead links" in out["verdict"]


def test_applying_but_aimed_wrong_is_still_a_problem():
    # Two applications is not success when 40 postings were thrown away to get them.
    out = _report({"opened": 42, "applied": 2, "skipped_fit": 40}, minutes=45)
    assert "aimed wrong" in out["verdict"]


def test_stale_token_outranks_every_other_reading():
    # A 401 explains all the other zeros; reporting "no apply button" would mislead.
    out = _report({"opened": 12, "skipped_no_button": 12}, minutes=10, auth_401=3)
    assert "401" in out["verdict"]


def test_a_healthy_run_says_so_with_numbers():
    out = _report({"opened": 14, "applied": 12}, minutes=24)
    assert out["verdict"].startswith("Healthy")
    assert out["minutes_per_application"] == 2.0


def test_slow_is_separated_from_broken():
    out = _report({"opened": 6, "applied": 2}, minutes=60)
    assert "Slow" in out["verdict"]
    assert out["applications_per_hour"] == 2.0


def test_silence_is_reported_as_silence():
    out = _report({}, minutes=0, total=0)
    assert "nothing has been running" in out["verdict"].lower()


def test_yield_is_none_rather_than_a_divide_by_zero():
    out = _report({"opened": 5}, minutes=3)
    assert out["minutes_per_application"] is None
    assert out["applications_per_hour"] is None  # under 5 min = not enough to rate


def test_a_dom_change_is_named_not_scattered_into_other_buckets():
    # First live use of this report (2026-09-11) had exactly this blind spot: Indeed
    # rebuilt /viewjob, dozens of postings logged "No job title on this page" — and the
    # verdict blamed 4 dead links, because the dominant loss wasn't a category at all.
    # A loss the report cannot name is a loss it will misattribute.
    out = _report({"opened": 15, "page_unreadable": 30, "dead_link": 4}, minutes=22)
    assert "page changed under us" in out["verdict"]


def test_the_live_log_line_maps_to_the_category():
    assert (
        activity_db._categorize("⏭️ No job title on this page (/viewjob) — skipping to the next job")
        == "page_unreadable"
    )
