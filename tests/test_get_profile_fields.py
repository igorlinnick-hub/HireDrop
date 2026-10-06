"""A column the readers use has to come back out of get_profile().

get_profile() returns a hand-built dict, not the row. Until 09-30 five columns were
written (update_salary / update_profile / update_ats) but never returned, so every
reader saw None: the salary and work-setting gates in on_search_filter never fired, and
resume_text_for() never read the resume the user corrected in the editor (#222). The
tests for those features patched get_profile with dicts that already carried the keys —
which is exactly how it stayed hidden. These tests run the REAL get_profile over a fake
Supabase row and hand its output to the real readers.
"""

from unittest.mock import MagicMock, patch

from app.db.profile import get_profile
from app.routers.jobs import on_search_filter
from modules.ai_cover_letter import resume_text_for
from modules.ats_pdf_generator import structure_to_text

STRUCTURE = {
    "name": "Jane Roe",
    "title": "Social Media Manager",
    "contact": {"phone": "", "email": "jane@example.com", "location": "Austin, TX"},
    "summary": "Social media manager with six years running paid and organic programs "
    "for consumer brands, from content calendars to influencer partnerships.",
    "competencies": ["Content strategy", "Paid social", "Community management"],
    "experience": [
        {
            "title": "Social Media Manager",
            "company": "Corrected Company Name",
            "location": "Austin, TX",
            "dates": "2021 – 2025",
            "bullets": ["Grew the account from 12k to 140k", "Ran a $40k/month paid budget"],
        }
    ],
    "education": [{"degree": "BA Communications", "school": "UT Austin", "year": "2018"}],
}


def _profile_from_row(row: dict | None) -> dict:
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[row] if row is not None else []
    )
    with patch("app.db.profile.get_supabase", return_value=client):
        return get_profile("user-1")


def _row(**extra) -> dict:
    return {"user_id": "user-1", "keywords": ["engineer"], "location": "", **extra}


def test_the_search_gate_columns_come_back():
    p = _profile_from_row(
        _row(salary_min=100_000, salary_max=180_000, salary_listed_only=True, work_setting="remote")
    )
    assert p["salary_min"] == 100_000
    assert p["salary_max"] == 180_000
    assert p["salary_listed_only"] is True
    assert p["work_setting"] == "remote"


def test_the_salary_floor_reaches_the_deck_filter():
    profile = _profile_from_row(_row(salary_min=100_000))
    low = {
        "title": "Engineer",
        "location": "Remote",
        "job_type": None,
        "description": "The base pay for this role is: $64,832 - $85,092 per year.",
    }
    high = {**low, "description": "The base pay for this role is: $135,792 - $178,227 per year."}
    assert on_search_filter([low, high], profile) == [high]


def test_the_corrected_resume_reaches_the_ai():
    profile = _profile_from_row(_row(default_resume="ats", ats_structure=STRUCTURE))
    text = resume_text_for(profile)
    assert text == structure_to_text(STRUCTURE)[: len(text)]
    assert "Corrected Company Name" in text


def test_unset_columns_read_as_no_preference():
    # A profile that never touched these must not grow a filter out of nowhere.
    for p in (_profile_from_row(_row()), _profile_from_row(None)):
        assert p["salary_min"] is None
        assert p["salary_max"] is None
        assert p["salary_listed_only"] is False
        assert p["work_setting"] == ""
        assert p["ats_structure"] is None


# ── A resume path is only ever the user's own ───────────────────────────────────────
# profiles.resume_url is written by the browser, and RLS lets a user set it to anything.
# The server reads the path with the service key — so "anything" used to include another
# user's file.

ME, VICTIM = "user-1", "9b2f6c1e-0000-4000-8000-000000000042"


def test_a_path_inside_the_users_folder_is_theirs():
    assert _profile_from_row(_row(resume_url=f"{ME}/Jane_Roe_Resume.pdf"))["resume_url"] == (
        f"{ME}/Jane_Roe_Resume.pdf"
    )


def test_a_path_in_someone_elses_folder_is_no_resume_at_all():
    for stolen in (
        f"{VICTIM}/resume.pdf",
        f"{ME}/../{VICTIM}/resume.pdf",
        f"{ME}\\..\\{VICTIM}\\resume.pdf",
        f"/{ME}/resume.pdf",
        "resume.pdf",
        f"{ME}x/resume.pdf",
    ):
        assert _profile_from_row(_row(resume_url=stolen))["resume_url"] == "", stolen


def test_the_signed_download_never_resolves_to_a_foreign_file():
    """The reader that mattered most: GET /profile/resume/url signs whatever
    resolved_resume_path returns."""
    from app.db import resume as resume_storage

    looked_up: list[str] = []

    def exists(path):
        looked_up.append(path)
        return path == f"{VICTIM}/resume.pdf"  # the victim's file is really there

    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.execute.return_value = MagicMock(
        data=[_row(resume_url=f"{VICTIM}/resume.pdf")]
    )
    with (
        patch("app.db.profile.get_supabase", return_value=client),
        patch.object(resume_storage, "_object_exists", side_effect=exists),
    ):
        assert resume_storage.resolved_resume_path(ME) is None
    assert f"{VICTIM}/resume.pdf" not in looked_up


def test_resume_facts_come_back_and_are_none_before_the_column_exists():
    stored = {"resume_url": "user-1/r.pdf", "facts": {"school": "UH"}}
    assert _profile_from_row(_row(resume_facts=stored))["resume_facts"] == stored
    assert _profile_from_row(_row())["resume_facts"] is None
    assert _profile_from_row(None)["resume_facts"] is None
