"""The form's questions and our answers ride with the application and come back in
History; a malformed copy never costs the application row."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.db import applications as apps_db
from app.routers import applications as router
from app.schemas import ApplicationSaveRequest

USER = SimpleNamespace(id="u1", email="a@b.c")


def test_request_clips_instead_of_refusing():
    req = ApplicationSaveRequest(
        job_title="t",
        company="c",
        form_answers=[{"q": "Q" * 999, "a": "A" * 999}, "junk"] + [{"q": "x", "a": "y"}] * 80,
    )
    assert len(req.form_answers) == 60
    assert len(req.form_answers[0].q) == 200 and len(req.form_answers[0].a) == 500


def test_save_passes_the_answers_to_the_row():
    qa = [{"q": "Willing to work 5 days in the office?", "a": "Yes."}]
    req = ApplicationSaveRequest(job_title="t", company="c", job_url="u", form_answers=qa)
    with (
        patch.object(
            router,
            "check_can_apply",
            return_value={
                "allowed": True,
                "tier": "pro",
                "used_today": 0,
                "daily_limit": 50,
                "free_used": 0,
                "free_limit": 40,
            },
        ),
        patch.object(router.jobs_db, "save_job", return_value="j1"),
        patch.object(router.jobs_db, "mark_applied_by_link"),
        patch.object(router.apps_db, "save_application") as save,
        patch.object(router.handbacks_db, "resolve_for_posting"),
        patch.object(router.meta_capi, "track_start_trial"),
    ):
        router.save_application(req, USER)
    assert save.call_args.kwargs["form_answers"] == qa


def test_row_carries_answers_only_when_there_are_some():
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.return_value.data = [{"id": "a1"}]
    with patch.object(apps_db, "get_supabase", return_value=sb):
        apps_db.save_application("u1", "j1", form_answers=[{"q": "q", "a": "a"}])
        assert sb.table.return_value.insert.call_args[0][0]["form_answers"] == [
            {"q": "q", "a": "a"}
        ]
        apps_db.save_application("u1", "j1")
        assert "form_answers" not in sb.table.return_value.insert.call_args[0][0]
