"""The one-time check before the first run (modules/review_sheet.py).

What this protects: the person sees, once, every value a form gets from their profile —
including the two the filler would otherwise send without asking ("2 weeks", "Fluent") —
and only an account that never ran is held to it. An account that already ran must never
be stopped to re-confirm, and a read that fails must never hold Start shut.
"""

from unittest.mock import MagicMock, patch

from app.db import campaign as campaign_db
from app.db.campaign import build_readiness
from app.routers import campaign as campaign_router
from modules import review_sheet
from modules.employer_answers import QUESTIONS
from tests.test_employer_answers_api import READY

CONTACT = {"name": "Jordan", "last_name": "Avery", "phone": "(512) 555-0147"}
PROFILE = {**READY, **CONTACT}


def _rows(sections):
    return [row for s in sections for row in s["rows"]]


def test_the_sheet_holds_every_employer_question_once_plus_contact_and_defaults():
    rows = _rows(review_sheet.sheet(PROFILE, "jordan@example.com"))
    keys = [r["key"] for r in rows]
    for key, _label, _kind in QUESTIONS:
        assert keys.count(key) == 1, key
    for key in ("name", "last_name", "phone", "email", "notice_period", "english_level", "eeo"):
        assert key in keys
    by_key = {r["key"]: r for r in rows}
    assert by_key["email"] == {
        "key": "email",
        "label": "Email",
        "kind": "info",
        "value": "jordan@example.com",
    }
    assert by_key["name"]["value"] == "Jordan" and by_key["name"]["required"] is True
    assert by_key["postal_code"]["required"] is False


def test_blank_defaulted_rows_show_what_the_filler_would_send():
    by_key = {r["key"]: r for r in _rows(review_sheet.sheet(PROFILE, None))}
    assert by_key["notice_period"]["value"] == "2 weeks"
    assert by_key["english_level"]["value"] == "Fluent"
    mine = {
        r["key"]: r
        for r in _rows(review_sheet.sheet({**PROFILE, "notice_period": "1 month"}, None))
    }
    assert mine["notice_period"]["value"] == "1 month"


def test_employer_rows_keep_their_opt_outs_and_values():
    by_key = {
        r["key"]: r for r in _rows(review_sheet.sheet({**PROFILE, "no_linkedin": True}, None))
    }
    assert by_key["country"]["kind"] == "us_resident" and by_key["country"]["value"] is True
    assert by_key["work_authorized_us"]["value"] is True
    assert by_key["linkedin_url"]["opt_out"]["flag"] == "no_linkedin"


def test_clean_keeps_only_the_sheets_own_text_keys():
    out = review_sheet.clean(
        {"name": "  Jordan ", "phone": "x" * 500, "keywords": ["a"], "answers_confirmed_at": "now"}
    )
    assert out == {"name": "Jordan", "phone": "x" * 200}


def test_incomplete_names_blank_contact_rows():
    assert review_sheet.incomplete(PROFILE) == []
    assert review_sheet.incomplete({**PROFILE, "phone": " "}) == ["phone"]


# ── who owes it ──────────────────────────────────────────────────────────────


def test_a_confirmed_account_owes_nothing_and_reads_nothing():
    with patch.object(campaign_db, "ran_before") as ran:
        assert (
            campaign_db.review_due("u1", {"answers_confirmed_at": "2026-10-07T10:00:00Z"}) is False
        )
    ran.assert_not_called()


def test_an_account_that_ever_started_owes_nothing():
    assert campaign_db.review_due("u1", {}, {"started_at": "2026-09-01T00:00:00Z"}) is False


def _applications(rows):
    sb = MagicMock()
    sb.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = rows
    return patch.object(campaign_db, "get_supabase", return_value=sb)


def test_runs_without_a_start_still_count_as_having_run():
    with _applications([{"id": "a1"}]):
        assert campaign_db.review_due("u1", {}, {"started_at": None}) is False


def test_a_brand_new_account_owes_it():
    with _applications([]):
        assert campaign_db.review_due("u1", {}, {"started_at": None}) is True


def test_the_history_read_is_scoped_to_the_account_and_one_row():
    with _applications([]) as gs:
        campaign_db.ran_before("u1", {"started_at": None})
    chain = gs.return_value.table.return_value.select.return_value
    chain.eq.assert_called_once_with("user_id", "u1")
    chain.eq.return_value.limit.assert_called_once_with(1)


def test_a_failed_read_never_holds_start():
    with patch.object(campaign_db, "get_supabase", side_effect=RuntimeError("down")):
        assert campaign_db.review_due("u1", {}, {"started_at": None}) is False


# ── the gate ─────────────────────────────────────────────────────────────────


def _checks(res):
    return {c["id"]: c for c in res["checks"]}


def test_readiness_shows_the_row_only_to_a_client_that_can_draw_it():
    owed = build_readiness(PROFILE, False, "pro", "auto", None, 40, 3, review_due=True)
    assert owed["ready"] is False
    assert _checks(owed)["review"]["fix"] == "review"
    old_tab = build_readiness(PROFILE, False, "pro", "auto", None, 40, 2, review_due=True)
    assert "review" not in _checks(old_tab) and old_tab["ready"] is True
    done = build_readiness(PROFILE, False, "pro", "auto", None, 40, 3, review_due=False)
    assert done["ready"] is True and _checks(done)["review"]["ok"] is True


def _start(client, profile, due, ui=None):
    path = "/api/v1/campaign/start" + (f"?answers_ui={ui}" if ui else "")
    with (
        patch.object(campaign_router, "get_profile", return_value=profile),
        patch.object(campaign_router.campaign_db, "review_due", return_value=due),
        patch.object(campaign_router.campaign_db, "start", return_value={"running": True}),
        patch.object(campaign_router, "get_submit_mode", return_value="auto"),
    ):
        return client.post(path, json={"keywords": ["marketing"], "platforms": ["indeed"]})


def test_start_refuses_a_first_run_until_the_sheet_is_confirmed(auth_client):
    res = _start(auth_client, PROFILE, due=True)
    assert res.status_code == 403 and res.json()["detail"] == "review_missing"


def test_an_old_tab_is_not_refused_over_a_sheet_it_cannot_draw(auth_client):
    res = _start(auth_client, PROFILE, due=True, ui=2)
    assert res.status_code != 403


def test_missing_answers_are_named_before_the_review(auth_client):
    res = _start(auth_client, {**PROFILE, "school": ""}, due=True)
    assert res.json()["detail"] == "employer_answers_missing"


# ── the save ─────────────────────────────────────────────────────────────────


def _confirm(client, profile_after, body):
    update = MagicMock(return_value=profile_after)
    confirm = MagicMock()
    with (
        patch("app.routers.profile.profile_db.update_employer_answers", update),
        patch("app.routers.profile.profile_db.confirm_answers", confirm),
    ):
        res = client.post("/api/v1/profile/review?answers_ui=3", json=body).json()
    return res, update, confirm


def test_confirm_writes_the_edits_and_stamps_once_nothing_is_blank(auth_client):
    body = {"name": " Jordan ", "phone": "(512) 555-0147", "city": "Austin", "keywords": ["x"]}
    res, update, confirm = _confirm(auth_client, PROFILE, body)
    assert res == {"confirmed": True, "missing": [], "incomplete": []}
    written = update.call_args.args[1]
    assert written["name"] == "Jordan" and written["city"] == "Austin"
    assert "keywords" not in written
    confirm.assert_called_once()


def test_a_blank_row_keeps_the_edits_but_not_the_confirmation(auth_client):
    res, update, confirm = _confirm(
        auth_client, {**PROFILE, "phone": "", "school": ""}, {"phone": ""}
    )
    assert res["confirmed"] is False
    assert res["incomplete"] == ["phone"]
    assert [m["key"] for m in res["missing"]] == ["school"]
    update.assert_called_once()
    confirm.assert_not_called()


def test_the_sheet_says_whether_it_is_still_owed(auth_client):
    with (
        patch("app.routers.profile.profile_db.get_profile", return_value=PROFILE),
        patch("app.db.campaign.review_due", return_value=True),
    ):
        body = auth_client.get("/api/v1/profile/review").json()
    assert body["due"] is True
    assert [s["title"] for s in body["sections"]][0] == "Contact"
    assert body["no_linkedin"] is False
