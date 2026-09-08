"""Employment type — the picker that filtered against an empty column.

The dashboard has had Full-time / Part-time / Contract for months, but nothing ever wrote
`job_type` for ATS boards: 0 of 558 swipeable rows in Igor's pool carried one (measured
2026-09-08). So the filter could not have worked wherever it lived. These tests pin the
read (title first, deterministic, no AI) and the deck's use of it.
"""

from unittest.mock import patch

from app.routers import jobs as jobs_router
from modules.job_type import detect_job_type, matches_job_type


def test_the_board_usually_says_it_in_the_title():
    # The live card Igor swiped.
    assert detect_job_type("Marketing Media Strategist, International (Contract)") == "contract"
    assert detect_job_type("Part-Time Barista") == "part-time"
    assert detect_job_type("Summer Intern, Data") == "internship"


def test_the_title_outvotes_a_later_mention_in_the_description():
    # "full-time" turns up in benefits paragraphs of contract postings all the time.
    body = "Great perks for you and our full-time staff. Reports to the Head of Brand."
    assert detect_job_type("Brand Lead (Contract)", body) == "contract"


def test_the_narrower_type_wins_when_a_posting_claims_two():
    assert detect_job_type("Full-time contract engineer") == "contract"
    assert detect_job_type("Full-Time Internship, Growth") == "internship"


def test_silence_is_not_full_time():
    # None must stay distinct from a stated full-time job: a filter that reads silence as
    # a match is guessing on the user's behalf.
    assert detect_job_type("Brand Manager") is None
    assert detect_job_type("", "") is None


def test_unknown_type_passes_the_filter_and_a_stated_mismatch_does_not():
    assert matches_job_type(None, "full-time") is True  # pre-existing pool rows
    assert matches_job_type("contract", "full-time") is False
    assert matches_job_type("contract", None) is True  # no filter set
    assert matches_job_type("contract", "contract") is True


# --- the deck honours it ---------------------------------------------------------------


class _User:
    id = "u1"


def _row(title, job_type=None):
    return {
        "title": title,
        "location": "Miami, FL",
        "platform": "greenhouse",
        "status": "new",
        "link": f"https://boards.greenhouse.io/x/jobs/{abs(hash(title)) % 10**7}",
        "job_type": job_type,
    }


def test_deck_drops_a_stated_mismatch_and_keeps_the_untyped():
    pool = [
        _row("AI Engineer", "full-time"),
        _row("AI Engineer II", "contract"),
        _row("AI Engineer III"),  # harvested before the column was written
    ]
    with (
        patch.object(jobs_router.jobs_db, "get_jobs", return_value=pool),
        patch(
            "app.db.profile.get_profile",
            return_value={"keywords": ["ai engineer"], "job_type": "full-time"},
        ),
    ):
        out = jobs_router.get_deck(user=_User())

    assert [c["title"] for c in out["cards"]] == ["AI Engineer", "AI Engineer III"]
    assert out["job_type"] == "full-time"
    # Says how much of the pool simply predates the write, so an uninformed filter is not
    # mistaken for a broken one.
    assert out["untyped_rows"] == 1
