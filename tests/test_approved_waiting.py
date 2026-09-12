"""An approved swipe that nothing will apply to must be COUNTABLE.

Only a tap run reads `approved` rows; the auto walk searches platforms and never looks at
them. So a user who swiped and then ran (or switched back to) Auto has a stack that sits
there forever, and until now no surface could even ask how big it was. Found live 09-11:
a real free user has had 4 approved swipes waiting since 09-02, last seen 09-05.

/campaign/status now carries the count in every mode — the number is the precondition for
any surface saying "Auto won't apply these".
"""

from unittest.mock import patch

from app.db import jobs as jobs_db
from app.routers import campaign as campaign_router


def _status(auth_client, *, approved: int, mode: str = "auto"):
    with (
        patch.object(campaign_router.jobs_db, "count_approved_jobs", return_value=approved),
        patch.object(campaign_router.jobs_db, "count_new_jobs", return_value=0),
        patch.object(campaign_router, "read_submit_mode", return_value=mode),
        patch.object(campaign_router, "get_tier", return_value="pro"),
    ):
        return auth_client.get("/api/v1/campaign/status").json()


def test_status_reports_the_stranded_stack_in_auto_mode(auth_client):
    body = _status(auth_client, approved=4, mode="auto")
    assert body["approved_waiting"] == 4
    assert body["submit_mode"] == "auto"


def test_status_reports_it_in_tap_mode_too(auth_client):
    # In tap mode the same number is simply "what is left to apply" — one field, not two.
    assert _status(auth_client, approved=4, mode="tap")["approved_waiting"] == 4


def test_an_empty_deck_reports_zero_not_absence(auth_client):
    body = _status(auth_client, approved=0)
    assert body["approved_waiting"] == 0


def test_the_count_asks_the_db_for_approved_rows_only():
    with patch.object(jobs_db, "get_supabase") as gs:
        chain = (
            gs.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value
        )
        chain.execute.return_value.count = 4
        assert jobs_db.count_approved_jobs("u1") == 4
    # Head count, never a row fetch: /campaign/status is polled every few seconds.
    gs.return_value.table.return_value.select.assert_called_with("id", count="exact")


def test_a_null_count_is_zero_not_none():
    with patch.object(jobs_db, "get_supabase") as gs:
        chain = (
            gs.return_value.table.return_value.select.return_value.eq.return_value.eq.return_value
        )
        chain.execute.return_value.count = None
        assert jobs_db.count_approved_jobs("u1") == 0
