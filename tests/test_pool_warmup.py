"""POST /jobs/find-ats — the throttle that has to tell two questions apart.

The dashboard writes prefs the moment a chip is picked, and a warm-up sweep now rides on
that write, so the throttle can no longer be a plain per-user timer: the first chip of a
session would take the window with a half-built search (often before a single keyword was
saved) and the complete search, picked seconds later, would be refused as "ran recently" —
a warm-up that collects for the search the user abandoned and then reports success.

So: same search → the 10-minute cooldown; different search → a 60-second floor, which is
there so one moving chip can't become a board sweep per keystroke.
"""

from unittest.mock import patch

from app.routers import jobs as jobs_router


class _User:
    id = "u-warm"


def _call(profile):
    """find-ats with the sweep itself stubbed out — this pins the gate, not the sweep."""
    with (
        patch("app.db.profile.get_profile", return_value=profile),
        patch.object(jobs_router.threading, "Thread") as thread,
    ):
        out = jobs_router.find_ats_jobs(user=_User())
    # The sweep is stubbed, so nothing clears the in-progress guard the way
    # _run_ats_discovery's `finally` does in production. Clear it here, or every test past
    # the first call would be pinning a stuck flag instead of the cooldown.
    jobs_router._FIND_ATS_IN_PROGRESS.discard(_User.id)
    return out, thread


def setup_function():
    jobs_router._FIND_ATS_LAST_RUN.pop(_User.id, None)
    jobs_router._FIND_ATS_IN_PROGRESS.discard(_User.id)


def test_the_same_search_twice_sweeps_once():
    first, thread = _call({"keywords": ["event manager"], "location": "miami"})
    assert first["started"] is True
    assert thread.called

    second, thread = _call({"keywords": ["event manager"], "location": "miami"})
    assert second["started"] is False
    assert second["cooldown"] is True
    assert second["search_changed"] is False
    assert not thread.called  # nothing spawned — the pool already holds this answer


def test_reordering_the_same_chips_is_the_same_search():
    _call({"keywords": ["event manager", "operations"], "location": "miami"})
    out, thread = _call({"keywords": ["Operations", "Event Manager"], "location": "miami"})

    assert out["started"] is False
    assert out["search_changed"] is False
    assert not thread.called


def test_a_changed_search_is_not_made_to_wait_out_the_other_search_cooldown():
    _call({"keywords": ["event manager"], "location": "miami"})
    # Past the 60s floor, still deep inside the 10-minute cooldown the first search started.
    jobs_router._FIND_ATS_LAST_RUN[_User.id] = (
        jobs_router._FIND_ATS_LAST_RUN[_User.id][0],
        jobs_router._FIND_ATS_LAST_RUN[_User.id][1] - (jobs_router.NEW_SEARCH_MIN_GAP_SECS + 1),
    )

    out, thread = _call({"keywords": ["project manager"], "location": "miami"})

    assert out["started"] is True
    assert out["search_changed"] is True
    assert thread.called


def test_chips_moving_faster_than_the_floor_do_not_sweep_per_keystroke():
    _call({"keywords": ["event"], "location": "miami"})
    out, thread = _call({"keywords": ["event manager"], "location": "miami"})

    assert out["started"] is False
    assert out["search_changed"] is True
    assert 0 < out["retry_in_secs"] <= jobs_router.NEW_SEARCH_MIN_GAP_SECS
    assert not thread.called


def test_location_and_job_type_are_part_of_the_search():
    _call({"keywords": ["event manager"], "location": "miami", "job_type": "full-time"})
    jobs_router._FIND_ATS_LAST_RUN[_User.id] = (
        jobs_router._FIND_ATS_LAST_RUN[_User.id][0],
        jobs_router._FIND_ATS_LAST_RUN[_User.id][1] - (jobs_router.NEW_SEARCH_MIN_GAP_SECS + 1),
    )

    out, _ = _call({"keywords": ["event manager"], "location": "austin", "job_type": "full-time"})
    assert out["started"] is True


def test_a_sweep_already_running_is_never_duplicated():
    """The guard the stub normally hides: two sweeps for one account re-fetch and re-score
    the same boards, and scoring is the only paid step in the pipeline."""
    jobs_router._FIND_ATS_IN_PROGRESS.add(_User.id)
    try:
        with (
            patch("app.db.profile.get_profile", return_value={"keywords": ["event manager"]}),
            patch.object(jobs_router.threading, "Thread") as thread,
        ):
            out = jobs_router.find_ats_jobs(user=_User())
    finally:
        jobs_router._FIND_ATS_IN_PROGRESS.discard(_User.id)

    assert out["started"] is False
    assert not thread.called
