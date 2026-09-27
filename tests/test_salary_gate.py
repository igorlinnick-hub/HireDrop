"""The salary preference has to REACH the two paths that spend a slot.

Why this file exists as its own test: the filter existed in modules/salary_filter.py the
whole time and was called from nowhere on any live path — a user set "$150k minimum" in the
launch modal and the deck and the auto ATS queue ignored it (verified 09-25,
docs/handoff/salary-filter.md). Unit tests on the parser cannot catch that; only a test
that drives the gate can.
"""

from unittest.mock import patch

from app.routers.jobs import on_search_filter

PROFILE = {"keywords": ["engineer"], "job_type": "", "location": ""}

# A row shaped like the pool: pay lives in the description, nowhere else (there is no
# salary column — project_salary_not_stored).
LOW = {
    "title": "Engineer",
    "location": "Remote",
    "job_type": None,
    "description": "Pay Transparency: The base pay for this role is: $64,832 - $85,092 per year.",
}
HIGH = {
    "title": "Engineer",
    "location": "Remote",
    "job_type": None,
    "description": "The base pay for this role is: $135,792 - $178,227 per year.",
}
SILENT = {
    "title": "Engineer",
    "location": "Remote",
    "job_type": None,
    "description": "Join a fast-growing team. We raised $100M in Series C financing.",
}


def test_a_floor_drops_the_posting_that_states_less():
    out = on_search_filter([LOW, HIGH], {**PROFILE, "salary_min": 100_000})
    assert [j["description"][:30] for j in out] == [HIGH["description"][:30]]


def test_a_ceiling_drops_the_posting_that_states_more():
    out = on_search_filter([LOW, HIGH], {**PROFILE, "salary_max": 100_000})
    assert out == [LOW]


def test_a_posting_that_states_no_pay_still_shows():
    # Measured (scripts/measure_salary_fill.py, 09-26): ~88% of rows state no readable pay,
    # and rejecting them left 6% of the deck and 1 of 124 real applications.
    out = on_search_filter([SILENT], {**PROFILE, "salary_min": 150_000})
    assert out == [SILENT]


def test_listed_only_is_the_users_own_opt_out():
    out = on_search_filter(
        [SILENT, HIGH], {**PROFILE, "salary_min": 100_000, "salary_listed_only": True}
    )
    assert out == [HIGH]


def test_no_preference_touches_nothing():
    rows = [LOW, HIGH, SILENT]
    assert on_search_filter(rows, PROFILE) == rows


def test_the_auto_ats_queue_uses_the_same_gate():
    """The queue the auto campaign walks — the path that actually submits. It shares
    on_search_filter with the deck precisely so the two can't drift (#113 lesson)."""
    from app.routers import jobs as jobs_router

    class _User:
        id = "u1"

    rows = [
        {
            **LOW,
            "platform": "greenhouse",
            "status": "new",
            "link": "https://job-boards.greenhouse.io/acme/jobs/1",
        },
        {
            **HIGH,
            "platform": "greenhouse",
            "status": "new",
            "link": "https://job-boards.greenhouse.io/acme/jobs/2",
        },
    ]
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=rows),
        patch("app.db.profile.get_profile", return_value={**PROFILE, "salary_min": 100_000}),
        patch.object(jobs_router, "is_zero_touch", return_value=True),
        patch.object(jobs_router, "fresh_enough", return_value=True),
    ):
        out = jobs_router.get_ats_queue(platform="greenhouse", user=_User())
    links = [j["link"] for j in out["jobs"]]
    assert links == ["https://job-boards.greenhouse.io/acme/jobs/2"]
