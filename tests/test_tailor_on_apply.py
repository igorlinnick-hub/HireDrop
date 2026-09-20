"""Tailoring follows the application, not a second opinion about the job.

The gate used to be `jobs.score >= 6/7/8 by apply mode`. It was a vestige: when
tailoring ran eagerly at discovery, jobs.score was the only signal there was, and
when commit 391558b moved tailoring to apply time the gate did not move with it.

It was never derived from anything either — ceab921 says "was hardcoded 7" — and
measured on 2026-09-15 it let through 2 of 719 real described jobs, leaving
`tailored_resume` set on ONE row in 1307 for a feature the landing page sells.

Reaching _lazy_tailor_for_job already means we are applying: its only caller is
GET /profile/resume/url/best, whose only caller is content.js uploadResume(),
filling the form we are about to submit.

The gate that replaced it (2026-09-20) asks whether there is a JOB to tailor to.
Indeed's `description` holds the search-card snippet — measured on the live pool,
0 of 387 Indeed rows carry real posting text, and three resumes were tailored from
strings like "From $40,000 a yearFull-time" the day the score gate came off.

What must survive: the paid-tier gate, idempotency (never pay twice for one job),
and best-effort (a tailoring failure must never break the resume fetch — the
application still needs a resume).
"""

from unittest.mock import MagicMock, patch

import pytest

from app.routers import profile as profile_router
from app.routers.profile import MIN_TAILORABLE_DESC

# A real ATS posting, shortened. The shortest greenhouse/lever/ashby row in the live
# pool is ~1100 chars; the fixture used to carry 32, which the new gate correctly
# refuses — a resume cannot be "targeted" at one sentence.
REAL_POSTING = (
    "Own delivery across three teams. You will run planning, keep the roadmap honest, "
    "and be the person engineering and design both trust with scope. We work in "
    "two-week cycles, ship behind flags, and expect written proposals before large "
    "changes. You should be comfortable saying no to a stakeholder and explaining why "
    "in terms of the customer. Five years of delivery experience, ideally in a "
    "product company where you owned outcomes rather than tickets."
)


@pytest.fixture
def job_row():
    return {
        "id": "job-1",
        "title": "Project Manager",
        "company": "Corvant",
        "description": REAL_POSTING,
        "score": 1,  # the score that used to veto everything
        "tailored_resume": None,
        "tailored_resume_pdf_url": None,
    }


def _run(user_id="u1", email="x@y.z", tier="pro", job=None, tailored="TAILORED RESUME"):
    """Drive _lazy_tailor_for_job with everything around it stubbed."""
    user = type("U", (), {"id": user_id, "email": email})()
    tailor = MagicMock(return_value=tailored)
    update = MagicMock()
    with (
        patch("app.db.jobs.get_job_by_id", return_value=job),
        patch("app.db.jobs.update_tailored_resume", update),
        patch("app.db.subscriptions.get_tier", return_value=tier),
        patch("app.db.profile.get_profile", return_value={"resume_url": "r.pdf"}),
        patch("modules.ai_cover_letter.load_resume_text", return_value="base resume"),
        patch("modules.ai_resume_tailor.tailor_resume", tailor),
        patch.object(profile_router, "_store_tailored_pdf", MagicMock()),
    ):
        profile_router._lazy_tailor_for_job(user, job)
    return tailor, update


def test_a_score_1_job_is_tailored_now(job_row):
    """The whole point: we are applying, so the application gets a tailored resume."""
    tailor, update = _run(job=job_row)
    tailor.assert_called_once()
    update.assert_called_once()


@pytest.mark.parametrize("score", [None, 0, 1, 5, 9])
def test_the_score_no_longer_decides_anything(job_row, score):
    """Every score tailors — including the ones the old 6/7/8 gate rejected."""
    tailor, _ = _run(job={**job_row, "score": score})
    assert tailor.called, f"score={score} must not veto an application we are sending"


def test_free_tier_still_does_not_tailor(job_row):
    """Removing the score gate must not remove the tier gate with it."""
    tailor, _ = _run(tier="free", job=job_row)
    tailor.assert_not_called()


def test_already_tailored_job_is_not_paid_for_twice(job_row):
    tailor, _ = _run(job={**job_row, "tailored_resume_pdf_url": "https://x/y.pdf"})
    tailor.assert_not_called()


def test_a_tailoring_failure_never_breaks_the_resume_fetch(job_row):
    """The application still needs a resume — a failed tailor must be swallowed."""
    user = type("U", (), {"id": "u1", "email": "x@y.z"})()
    with (
        patch("app.db.jobs.get_job_by_id", return_value=job_row),
        patch("app.db.subscriptions.get_tier", return_value="pro"),
        patch("app.db.profile.get_profile", return_value={"resume_url": "r.pdf"}),
        patch("modules.ai_cover_letter.load_resume_text", return_value="base resume"),
        patch("modules.ai_resume_tailor.tailor_resume", side_effect=RuntimeError("down")),
    ):
        profile_router._lazy_tailor_for_job(user, job_row)  # must not raise


def test_empty_model_output_is_not_stored(job_row):
    """An empty tailor result would overwrite a good resume with nothing."""
    _, update = _run(job=job_row, tailored="")
    update.assert_not_called()


# ── The gate that replaced the score: is there a job to tailor TO? ───────────


def test_an_indeed_snippet_is_not_a_job_description(job_row):
    """The live failure: Sonnet paid to target a salary string.

    Three resumes were tailored from descriptions of 24-28 chars on 2026-09-20.
    The output was fluent and entirely invented — worse than shipping the user's
    own resume, because we paid for it and sent it to an employer.
    """
    tailor, update = _run(job={**job_row, "description": "From $40,000 a yearFull-time"})
    tailor.assert_not_called()
    update.assert_not_called()


@pytest.mark.parametrize(
    "desc", [None, "", "   ", "Marketing Manager", "x" * (MIN_TAILORABLE_DESC - 1)]
)
def test_thin_text_never_reaches_the_model(job_row, desc):
    tailor, _ = _run(job={**job_row, "description": desc})
    assert not tailor.called, f"{len(desc or '')} chars is not something to tailor toward"


def test_a_real_posting_still_tailors(job_row):
    """The gate must not become the new 'nothing ever tailors'."""
    assert len(REAL_POSTING) >= MIN_TAILORABLE_DESC
    tailor, update = _run(job=job_row)
    tailor.assert_called_once()
    update.assert_called_once()


def test_the_cheap_check_runs_before_the_paid_one(job_row):
    """A thin job must not even cost a tier lookup round-trip."""
    user = type("U", (), {"id": "u1", "email": "x@y.z"})()
    tier = MagicMock(return_value="pro")
    with (
        patch("app.db.jobs.get_job_by_id", return_value={**job_row, "description": "thin"}),
        patch("app.db.subscriptions.get_tier", tier),
    ):
        profile_router._lazy_tailor_for_job(user, job_row)
    tier.assert_not_called()
