"""The employer-answers endpoints: what signup draws, and when the resume is read.

One list (modules/employer_answers.py) drives signup, the Start gate and the Start
refusal. These tests pin the two things that could cost someone: a model call fired for
a form the resume could not help with, and a suggestion quietly written as an answer.
"""

from unittest.mock import MagicMock, patch

import pytest

ANSWERED = {
    "country": "United States",
    "city": "Honolulu",
    "state": "HI",
    "work_authorized_us": True,
    "needs_sponsorship": False,
    "current_title": "Marketing Manager",
    "current_employer": "Acme",
    "linkedin_url": "https://linkedin.com/in/x",
    "school": "UT Austin",
    "degree": "BA",
    "salary_expectation": "$90,000",
}
STRUCTURE = {
    "experience": [{"title": "Marketing Lead", "company": "Hawaii Wellness Clinic"}],
    "education": [{"degree": "B.S. in Economics", "school": "Kharkiv National University"}],
}


@pytest.fixture(autouse=True)
def _fresh_memo():
    from modules import ai_resume_facts

    ai_resume_facts._MEMO.clear()
    yield
    ai_resume_facts._MEMO.clear()


NOT_CALLED = object()


def _suggest(client, profile, found=NOT_CALLED, resume_text="resume text", seen=None):
    """POST /suggest with the model faked: `found` is what extract_facts answers.
    `seen`, when given, receives the whole body and the save / load mocks."""
    extract = MagicMock(return_value={} if found is NOT_CALLED else found)
    claim = MagicMock(return_value=True)
    release = MagicMock()
    save = MagicMock(return_value=True)
    load = MagicMock(return_value=resume_text)
    with (
        patch("app.routers.profile.profile_db.get_profile", return_value=profile),
        patch("app.routers.profile.profile_db.save_resume_facts", save),
        patch("modules.ai_resume_facts.extract_facts", extract),
        patch("modules.ai_cover_letter.load_resume_text", load),
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.usage_db.claim_today", claim),
        patch("app.db.usage.release_today", release),
    ):
        res = client.post("/api/v1/profile/employer-answers/suggest", json={})
    assert res.status_code == 200, res.text
    if seen is not None:
        seen.update(body=res.json(), save=save, load=load)
    return res.json()["suggestions"], extract, claim, release


def test_signup_gets_every_question_with_what_is_on_file(auth_client):
    profile = {**ANSWERED, "school": "", "degree": "", "no_degree": True}
    with patch("app.routers.profile.profile_db.get_profile", return_value=profile):
        body = auth_client.get("/api/v1/profile/employer-answers").json()
    keys = [q["key"] for q in body["questions"]]
    assert keys[0] == "country" and "school" in keys and "salary_expectation" in keys
    assert body["missing"] == []
    assert body["no_degree"] is True and body["no_linkedin"] is False
    assert body["no_salary_expectation"] is False


def test_the_generated_ats_resume_is_never_the_source(auth_client):
    """ats_structure is a model's rewrite, possibly of an earlier upload: its title is
    not necessarily the one the person held. Only the uploaded PDF is read."""
    profile = {
        **ANSWERED,
        "current_title": "",
        "school": "",
        "degree": "",
        "ats_structure": STRUCTURE,
        "resume_url": "u/resume-2.pdf",
    }
    seen: dict = {}
    hints, extract, claim, _ = _suggest(
        auth_client, profile, found={"current_title": "Marketing Coordinator"}, seen=seen
    )
    assert hints == {"current_title": "Marketing Coordinator"}
    assert seen["body"]["from_resume"] == ["current_title"]
    extract.assert_called_once()
    claim.assert_called_once()


def test_the_question_rows_never_carry_the_ats_rewrite(auth_client):
    """A row with a suggestion is a box the form never asks /suggest about: the rewrite's
    title would stay in it and be confirmed as what the resume says."""
    profile = {**ANSWERED, "current_title": "", "school": "", "ats_structure": STRUCTURE}
    with patch("app.routers.profile.profile_db.get_profile", return_value=profile):
        body = auth_client.get("/api/v1/profile/employer-answers").json()
    for rows in (body["questions"], body["missing"]):
        by_key = {r["key"]: r for r in rows}
        assert "suggestion" not in by_key["current_title"]
        assert "suggestion" not in by_key["school"]


def test_an_ats_resume_without_an_upload_suggests_nothing(auth_client):
    profile = {**ANSWERED, "school": "", "degree": "", "ats_structure": STRUCTURE}
    hints, extract, claim, _ = _suggest(auth_client, profile)
    assert hints == {}
    extract.assert_not_called()
    claim.assert_not_called()


def test_facts_read_from_this_resume_are_reused_for_free(auth_client):
    """Stored on the profile with the resume they came from: any worker, any deploy."""
    profile = {
        **ANSWERED,
        "school": "",
        "degree": "",
        "resume_url": "u/resume-2.pdf",
        "resume_facts": {
            "resume_url": "u/resume-2.pdf",
            "facts": {"school": "Kharkiv National University", "city": "Honolulu"},
            "extracted_at": "2026-10-06T00:00:00+00:00",
        },
    }
    seen: dict = {}
    hints, extract, claim, _ = _suggest(auth_client, profile, seen=seen)
    # Only what is still blank is offered — city is answered and not second-guessed.
    assert hints == {"school": "Kharkiv National University"}
    assert seen["body"]["from_resume"] == ["school"]
    extract.assert_not_called()
    claim.assert_not_called()
    seen["save"].assert_not_called()
    seen["load"].assert_not_called()  # not even the PDF is downloaded


def test_facts_from_a_replaced_resume_are_read_again_and_stored(auth_client):
    profile = {
        **ANSWERED,
        "school": "",
        "resume_url": "u/resume-3.pdf",
        "resume_facts": {"resume_url": "u/resume-2.pdf", "facts": {"school": "Old University"}},
    }
    seen: dict = {}
    hints, extract, claim, _ = _suggest(
        auth_client, profile, found={"school": "New University"}, seen=seen
    )
    assert hints == {"school": "New University"}
    extract.assert_called_once()
    claim.assert_called_once()
    user_id, saved = seen["save"].call_args[0]
    assert saved["resume_url"] == "u/resume-3.pdf"
    assert saved["facts"] == {"school": "New University"}
    assert saved["extracted_at"]


def test_a_read_that_never_answered_is_not_stored(auth_client):
    profile = {**ANSWERED, "school": "", "resume_url": "u/resume.pdf"}
    seen: dict = {}
    _suggest(auth_client, profile, found=None, seen=seen)
    seen["save"].assert_not_called()


def test_from_resume_never_names_the_salary_setting(auth_client):
    profile = {
        **ANSWERED,
        "degree": "",
        "salary_expectation": "",
        "salary_min": 100_000,
        "resume_url": "u/resume.pdf",
    }
    seen: dict = {}
    hints, *_ = _suggest(auth_client, profile, found={"degree": "BA"}, seen=seen)
    assert hints == {"degree": "BA", "salary_expectation": "$100,000 per year"}
    assert seen["body"]["from_resume"] == ["degree"]


def test_works_before_the_resume_facts_column_exists(auth_client):
    """Before migrations/add_resume_facts.sql: no key on the profile, and PostgREST
    refuses the write (PGRST204). The read still happens and is still remembered."""
    from app.db import profile as profile_db

    profile = {**ANSWERED, "school": "", "resume_url": "u/resume.pdf"}
    refusing = MagicMock()
    refusing.table.return_value.update.return_value.eq.return_value.execute.side_effect = Exception(
        "PGRST204: Could not find the 'resume_facts' column of 'profiles'"
    )
    extract = MagicMock(return_value={"school": "UT Austin"})
    with (
        patch("app.routers.profile.profile_db.get_profile", return_value=profile),
        patch.object(profile_db, "get_supabase", return_value=refusing),
        patch("modules.ai_resume_facts.extract_facts", extract),
        patch("modules.ai_cover_letter.load_resume_text", return_value="resume text"),
        patch("app.routers.tools.RATE_LIMIT_ENFORCE", True),
        patch("app.routers.tools.usage_db.claim_today", MagicMock(return_value=True)),
        patch("app.db.usage.release_today", MagicMock()),
    ):
        first = auth_client.post("/api/v1/profile/employer-answers/suggest", json={})
        again = auth_client.post("/api/v1/profile/employer-answers/suggest", json={})
    assert first.status_code == 200 and again.status_code == 200
    assert first.json() == {"suggestions": {"school": "UT Austin"}, "from_resume": ["school"]}
    assert again.json() == first.json()
    extract.assert_called_once()  # the process memo still covers the second ask


def test_a_bare_pdf_is_read_once_for_the_questions_it_can_answer(auth_client):
    # A salary floor is on file too: having ONE suggestion already must not talk the
    # endpoint out of reading the resume for the others.
    profile = {
        **ANSWERED,
        "school": "",
        "degree": "",
        "salary_expectation": "",
        "salary_min": 100_000,
        "resume_url": "u/resume.pdf",
    }
    found = {"school": "Kharkiv National University", "current_title": "Already answered"}
    hints, extract, claim, release = _suggest(auth_client, profile, found)
    # Only what is still blank comes back; an answered question is not second-guessed.
    assert hints == {
        "school": "Kharkiv National University",
        "salary_expectation": "$100,000 per year",
    }
    extract.assert_called_once()
    claim.assert_called_once()
    release.assert_not_called()


def test_no_model_call_when_the_resume_cannot_help(auth_client):
    """A salary figure is not in a resume: a blank one must not buy a read."""
    profile = {
        **ANSWERED,
        "salary_expectation": "",
        "salary_min": 100_000,
        "resume_url": "u/resume.pdf",
    }
    hints, extract, claim, _ = _suggest(auth_client, profile)
    assert hints == {"salary_expectation": "$100,000 per year"}
    extract.assert_not_called()
    claim.assert_not_called()


def test_a_read_that_never_answered_refunds_its_slot(auth_client):
    profile = {**ANSWERED, "school": "", "resume_url": "u/resume.pdf"}
    hints, extract, claim, release = _suggest(auth_client, profile, found=None)
    assert hints == {}
    claim.assert_called_once()
    release.assert_called_once()


def test_a_read_that_answered_with_nothing_usable_keeps_its_slot(auth_client):
    """Refunding this one made the endpoint an unlimited tap: 500 requests against a
    resume that states none of the facts were 500 paid calls and 0 quota used."""
    profile = {**ANSWERED, "school": "", "resume_url": "u/resume.pdf"}
    hints, extract, claim, release = _suggest(auth_client, profile, found={})
    assert hints == {}
    claim.assert_called_once()
    release.assert_not_called()
    # …and asking again costs nothing at all: the read is remembered.
    _hints, extract2, claim2, _release = _suggest(auth_client, profile, found={})
    extract2.assert_not_called()
    claim2.assert_not_called()


def test_a_new_resume_is_read_again(auth_client):
    profile = {**ANSWERED, "school": "", "resume_url": "u/resume.pdf"}
    _suggest(auth_client, profile, found={"school": "Old University"}, resume_text="old")
    hints, extract, claim, _ = _suggest(
        auth_client, profile, found={"school": "New University"}, resume_text="new"
    )
    assert hints == {"school": "New University"}
    extract.assert_called_once()
    claim.assert_called_once()


def test_saving_writes_only_known_keys_and_reports_what_is_left(auth_client):
    written = {}

    def _update(user_id, answers):
        written.update(answers)
        return {**ANSWERED, "school": "", "degree": "", "salary_expectation": "", **answers}

    with patch("app.routers.profile.profile_db.update_employer_answers", side_effect=_update):
        res = auth_client.post(
            "/api/v1/profile/employer-answers?answers_ui=2",
            json={"no_degree": True, "city": " Austin ", "tier": "pro"},
        )
        old = auth_client.post("/api/v1/profile/employer-answers", json={"city": "Austin"})
    # "No degree" clears the school it replaces; an unknown key is never written.
    assert written == {"no_degree": True, "school": "", "degree": "", "city": "Austin"}
    # The new form is told what is left of the whole list; an old one only of its own.
    assert [m["key"] for m in res.json()["missing"]] == ["salary_expectation"]
    assert old.json() == {"saved": True, "missing": []}


READY = {
    **ANSWERED,
    "onboarding_completed": True,
    "keywords": ["marketing"],
    "platforms": ["indeed"],
    "resume_url": "u/resume.pdf",
}


def _gate(client, profile, path="/api/v1/campaign/start"):
    """POST /campaign/start (or GET readiness) against `profile`, never reaching the DB."""
    with (
        patch("app.routers.campaign.get_profile", return_value=profile),
        patch(
            "app.routers.campaign.campaign_db.get_effective_state", return_value={"running": False}
        ),
        patch("app.routers.campaign.campaign_db.get_state", return_value={"filters": {}}),
        patch(
            "app.routers.campaign.campaign_db.start", side_effect=lambda _u, f: {"filters": f}
        ) as start,
        patch("app.routers.campaign.get_tier", return_value="pro"),
        patch("app.routers.campaign.get_submit_mode", return_value="auto"),
        patch("app.routers.campaign.resume_text_for", return_value=""),
    ):
        if "readiness" in path:
            return client.get(path), start
        return client.post(path, json={"keywords": ["marketing"], "platforms": ["indeed"]}), start


def test_start_and_readiness_hold_each_client_to_the_list_it_can_draw(auth_client):
    """A client that names an older list is held to it; saying nothing is the current list."""
    profile = {**READY, "school": "", "degree": "", "salary_expectation": ""}
    by_id = lambda r: {c["id"]: c for c in r.json()["checks"]}  # noqa: E731
    old, _ = _gate(auth_client, profile, "/api/v1/campaign/readiness?answers_ui=1")
    assert by_id(old)["employer_answers"]["ok"] is True
    for path in ("/api/v1/campaign/readiness?answers_ui=2", "/api/v1/campaign/readiness"):
        new, _ = _gate(auth_client, profile, path)
        assert by_id(new)["employer_answers"]["ok"] is False
        assert [m["key"] for m in by_id(new)["employer_answers"]["missing"]] == [
            "school",
            "degree",
            "salary_expectation",
        ]
    # The extension's own /campaign/start sends no answers_ui: it no longer gets through
    # on the old 8 questions.
    for path in ("/api/v1/campaign/start?answers_ui=2", "/api/v1/campaign/start"):
        refused, start = _gate(auth_client, profile, path)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "employer_answers_missing"
        start.assert_not_called()
    allowed, start = _gate(auth_client, profile, "/api/v1/campaign/start?answers_ui=1")
    assert allowed.status_code == 200 and start.call_count == 1


def test_start_refuses_without_a_resume_on_any_platform(auth_client):
    """No Start without a resume (Igor, 10-06) — not only for Greenhouse/Lever."""
    res, start = _gate(auth_client, {**READY, "resume_url": None})
    assert res.status_code == 403 and res.json()["detail"] == "resume_missing"
    start.assert_not_called()
    ok, start = _gate(auth_client, READY)
    assert ok.status_code == 200 and start.call_count == 1


def test_start_gate_order(auth_client):
    """Onboarding first, then the closed US door, then the resume, then the answers."""
    nothing = {**READY, "resume_url": "", "city": ""}
    cases = [
        ({**nothing, "onboarding_completed": False}, "onboarding_incomplete"),
        ({**nothing, "country": "Outside US"}, "us_only"),
        (nothing, "resume_missing"),
        ({**nothing, "resume_url": "u/resume.pdf"}, "employer_answers_missing"),
    ]
    for profile, detail in cases:
        res, _ = _gate(auth_client, profile)
        assert res.status_code == 403 and res.json()["detail"] == detail


def test_start_refuses_contradictory_work_authorization(auth_client):
    res, start = _gate(auth_client, {**READY, "work_authorized_us": False})
    assert res.status_code == 403 and res.json()["detail"] == "employer_answers_missing"
    start.assert_not_called()
    # Not authorized but needing sponsorship is a real answer and starts.
    ok, _ = _gate(auth_client, {**READY, "work_authorized_us": False, "needs_sponsorship": True})
    assert ok.status_code == 200


def test_signup_rows_carry_stage_and_note(auth_client):
    profile = {**ANSWERED, "work_authorized_us": False, "needs_sponsorship": False}
    with patch("app.routers.profile.profile_db.get_profile", return_value=profile):
        body = auth_client.get("/api/v1/profile/employer-answers").json()
    stage = {q["key"]: q["stage"] for q in body["questions"]}
    assert stage["country"] == "signup" and stage["school"] == "resume"
    noted = {q["key"] for q in body["questions"] if q.get("note")}
    assert noted == {"work_authorized_us", "needs_sponsorship"}
    assert [m["key"] for m in body["missing"]] == ["work_authorized_us", "needs_sponsorship"]
    assert all(m["note"] and m["stage"] == "signup" for m in body["missing"])
