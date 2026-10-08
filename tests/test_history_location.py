"""History rows carry where the job is — the dashboard's place filter reads these.

The jobs row is the only holder of `location`; the applications snapshot has none. A
row whose job never had a location must come back as an honest blank (empty text, no
place, no setting), never a guessed city.
"""

from unittest.mock import patch

from app.db import applications as apps_db
from app.db import resume as resume_storage


def _history(job: dict, job_title: str = "Marketing Manager"):
    row = {
        "id": "a1",
        "job_title": job_title,
        "company": "Acme",
        "platform": "indeed",
        "job_url": "https://www.indeed.com/viewjob?jk=1",
        "date_applied": "2026-10-07T05:30:40+00:00",
        "status": "applied",
        "jobs": job,
    }
    with (
        patch.object(apps_db, "fetch_paged", return_value=[row]),
        patch.object(resume_storage, "signed_url_from_path", side_effect=lambda p, u: f"S:{p}"),
    ):
        return apps_db.get_history("u1")[0]


def test_a_hybrid_houston_posting_reads_as_hybrid_in_houston():
    out = _history({"location": "Hybrid work in Houston, TX 77056"})
    assert out["location"] == "Hybrid work in Houston, TX 77056"
    assert out["place"] == "Houston, TX"
    assert out["work_setting"] == "hybrid"


def test_remote_wins_and_keeps_no_city():
    out = _history({"location": "Remote, US"})
    assert out["work_setting"] == "remote"
    assert out["place"] is None


def test_a_job_with_no_location_is_an_honest_blank():
    out = _history({"location": None})
    assert out == out | {"location": "", "place": None, "work_setting": None}


def test_the_title_still_says_remote_when_the_location_is_empty():
    out = _history({"location": ""}, job_title="Content Strategist (Remote)")
    assert out["work_setting"] == "remote"
    assert out["place"] is None


def test_a_deleted_jobs_row_does_not_break_the_row():
    out = _history(None)
    assert out["title"] == "Marketing Manager"
    assert out["place"] is None and out["work_setting"] is None
