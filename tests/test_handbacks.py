"""Hand-backs as a live to-do list — the applications waiting on the user's hands.

Born 09-21: the filler's hand-back existed only as an activity-log line, so the ONE
thing in the product that needs a human was the hardest thing to find. Igor: "это нужно
не в history а в попапе, чтоб была анимация заявки что нужно человеку доделать". Two
surfaces read this now (extension popup + dashboard rail), which is exactly why the
open/done state needs one owner.

These tests pin the two properties that make the list trustworthy: it counts JOBS not
attempts, and it drains.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.db import handbacks as hb

# conftest stubs open_urls for every test (the queue must not reach the network); these
# tests exercise the real one, captured at import — before that fixture runs.
_real_open_urls = hb.open_urls


@pytest.fixture(autouse=True)
def _no_build_read():
    """add() and open_urls() read the running build; a test that cares patches it itself."""
    with patch.object(hb, "_current_build", return_value=(None, None)):
        yield


class _User:
    id = "u1"
    email = "u1@example.com"


def _fake_supabase():
    """Chainable stub — records the filters the query actually applied."""
    calls = {"filters": [], "table": None, "payload": None, "conflict": None}
    tbl = MagicMock()

    def _remember(name):
        def f(*args, **kwargs):
            calls["filters"].append((name, args))
            return tbl

        return f

    tbl.select.side_effect = _remember("select")
    tbl.eq.side_effect = _remember("eq")
    tbl.is_.side_effect = _remember("is_")
    tbl.order.side_effect = _remember("order")
    tbl.limit.side_effect = _remember("limit")

    def _upsert(row, **kwargs):
        calls["payload"] = row
        calls["conflict"] = kwargs.get("on_conflict")
        return tbl

    tbl.upsert.side_effect = _upsert

    def _insert(row):
        calls["inserted"] = row
        calls["payload"] = row
        return tbl

    tbl.insert.side_effect = _insert

    def _update(patch_):
        calls["payload"] = patch_
        return tbl

    tbl.update.side_effect = _update

    tbl.execute.return_value = MagicMock(data=[{"id": "h1"}])
    client = MagicMock()

    def _table(name):
        calls["table"] = name
        return tbl

    client.table.side_effect = _table
    return client, calls, tbl


def test_open_list_is_scoped_to_the_user_and_hides_resolved():
    """service_role bypasses RLS, so a missing user_id filter hands over someone else's
    job links — the project's #1 risk class."""
    client, calls, _tbl = _fake_supabase()
    with patch.object(hb, "get_supabase", return_value=client):
        hb.list_open("u1")

    assert calls["table"] == "handbacks"
    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert ("is_", ("resolved_at", "null")) in calls["filters"]


def test_the_same_job_handed_back_twice_does_not_stack():
    """The list must count JOBS waiting, not attempts — a re-run that hits the same
    wall would otherwise inflate the badge and teach the user to ignore it."""
    client, calls, _tbl = _fake_supabase()  # the update finds the open row
    with patch.object(hb, "get_supabase", return_value=client):
        hb.add("u1", {"job_title": "Assoc. Director", "url": "https://x/form"})

    assert "inserted" not in calls
    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert ("eq", ("url", "https://x/form")) in calls["filters"]
    # Only the OPEN row — a resolved one stays history, the job starts a new row.
    assert ("is_", ("resolved_at", "null")) in calls["filters"]


def test_a_new_job_is_inserted_not_upserted():
    """The uniqueness is a PARTIAL index; Postgres rejects ON CONFLICT (user_id, url)
    against it with 42P10. That upsert made every POST /handbacks a 500 for six days
    while this mock happily accepted it — so pin that the path never goes back to it."""
    client, calls, tbl = _fake_supabase()
    tbl.execute.side_effect = [MagicMock(data=[]), MagicMock(data=[{"id": "h9"}])]
    with patch.object(hb, "get_supabase", return_value=client):
        row = hb.add("u1", {"job_title": "PM", "url": "https://x/new"})

    assert row == {"id": "h9"}
    assert calls["inserted"]["url"] == "https://x/new"
    assert not tbl.upsert.called


def test_a_racing_duplicate_becomes_an_update():
    """Two reports of one wall can both miss the lookup; the index admits the first,
    and the second must land as an update of it, not an error the extension swallows."""
    from postgrest.exceptions import APIError

    client, calls, tbl = _fake_supabase()
    dup = APIError({"code": "23505", "message": "duplicate key"})
    tbl.execute.side_effect = [MagicMock(data=[]), dup, MagicMock(data=[{"id": "h1"}])]
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb.add("u1", {"url": "https://x/form"}) == {"id": "h1"}


def test_resolve_is_scoped_and_only_touches_still_open_rows():
    client, calls, _tbl = _fake_supabase()
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb.resolve("u1", "h1") is True

    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert ("eq", ("id", "h1")) in calls["filters"]
    # Re-resolving must not move resolved_at a second time.
    assert ("is_", ("resolved_at", "null")) in calls["filters"]
    assert "resolved_at" in calls["payload"]


def test_resolving_something_that_isnt_yours_reports_plain_false():
    """A wrong id and someone else's id must answer identically — a distinguishable
    error confirms another user's row exists."""
    client, _, tbl = _fake_supabase()
    tbl.execute.return_value = MagicMock(data=[])  # no row matched
    with patch.object(hb, "get_supabase", return_value=client):
        assert hb.resolve("u1", "not-mine") is False


def test_long_fields_are_truncated_not_rejected():
    """A 10k-char reason from a weird form must not fail the write — losing the hand-back
    entirely is worse than losing the tail of its text."""
    client, calls, _tbl = _fake_supabase()
    with patch.object(hb, "get_supabase", return_value=client):
        hb.add("u1", {"job_title": "T" * 5000, "reason": "R" * 5000, "url": "u" * 5000})

    assert len(calls["payload"]["job_title"]) == 300
    assert len(calls["payload"]["reason"]) == 500
    assert len(calls["payload"]["url"]) == 1000


# --- a hand-back waits on the person only while the build that wrote it still runs ---


def _open_rows(*rows):
    return patch.object(hb, "fetch_paged", return_value=list(rows))


def test_a_newer_build_gets_one_retry_at_an_old_hand_back():
    """10-02: 1.8.30 taught the GH filler pay/school/state. Hand-backs left by 1.8.23
    must reach the queue again, or they wait on the person for questions we now answer."""
    rows = [
        {
            "url": "https://gh/old",
            "ext_version": "1.8.23",
            "created_at": "2026-10-01T10:00:00+00:00",
        },
        {
            "url": "https://gh/same",
            "ext_version": "1.8.30",
            "created_at": "2026-10-02T10:00:00+00:00",
        },
    ]
    with (
        _open_rows(*rows),
        patch.object(hb, "_current_build", return_value=("1.8.30", "2026-10-02T09:00:00+00:00")),
    ):
        assert _real_open_urls("u1", waiting_only=True) == ["https://gh/same"]


def test_rows_from_before_the_stamp_are_judged_by_when_the_build_first_pinged():
    rows = [
        {
            "url": "https://gh/before",
            "ext_version": None,
            "created_at": "2026-10-01T10:00:00+00:00",
        },
        {
            "url": "https://gh/after",
            "ext_version": None,
            "created_at": "2026-10-02T10:00:00.5+00:00",
        },
    ]
    with (
        _open_rows(*rows),
        patch.object(
            hb, "_current_build", return_value=("1.8.30", "2026-10-02T09:00:00.123456+00:00")
        ),
    ):
        assert _real_open_urls("u1", waiting_only=True) == ["https://gh/after"]


def test_an_unknown_build_keeps_every_hand_back_waiting():
    """No ping version (or an unreadable campaign_states): behave exactly as before."""
    rows = [
        {"url": "https://gh/a", "ext_version": "1.8.3", "created_at": "2026-09-01T00:00:00+00:00"}
    ]
    with _open_rows(*rows), patch.object(hb, "_current_build", return_value=(None, None)):
        assert _real_open_urls("u1", waiting_only=True) == ["https://gh/a"]


def test_the_night_shift_read_is_not_gated_by_builds():
    """open_urls() without waiting_only means "every open hand-back" — unchanged."""
    rows = [{"url": "https://gh/a"}]
    with _open_rows(*rows), patch.object(hb, "_current_build") as build:
        assert _real_open_urls("u1") == ["https://gh/a"]
    build.assert_not_called()


def test_a_repeat_hand_back_is_restamped_so_the_retry_cannot_loop():
    client, calls, _tbl = _fake_supabase()
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "_current_build", return_value=("1.8.30", "2026-10-02T09:00:00+00:00")),
    ):
        hb.add("u1", {"job_title": "Ops", "url": "https://gh/form"})
    assert calls["payload"]["ext_version"] == "1.8.30"


def test_a_missing_column_drops_the_stamp_not_the_hand_back():
    from postgrest.exceptions import APIError

    client, calls, tbl = _fake_supabase()
    tbl.execute.side_effect = [
        APIError({"code": "PGRST204", "message": "no ext_version column"}),
        MagicMock(data=[{"id": "h1"}]),
    ]
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "_current_build", return_value=("1.8.30", None)),
    ):
        assert hb.add("u1", {"url": "https://gh/form"}) == {"id": "h1"}
    assert "ext_version" not in calls["payload"]


def test_a_submitted_posting_leaves_the_to_do_list_by_identity():
    """The retry saves the posting URL; the hand-back holds the form URL of the same job."""
    client, calls, tbl = _fake_supabase()
    open_rows = [
        {"id": "h1", "url": "https://job-boards.greenhouse.io/doordashusa/jobs/8237299?gh_src=x"},
        {"id": "h2", "url": "https://job-boards.greenhouse.io/tia/jobs/8005735003"},
    ]
    tbl.in_ = MagicMock(return_value=tbl)
    with (
        patch.object(hb, "get_supabase", return_value=client),
        patch.object(hb, "fetch_paged", return_value=open_rows),
    ):
        n = hb.resolve_for_posting("u1", "https://boards.greenhouse.io/doordashusa/jobs/8237299")
    assert n == 1
    tbl.in_.assert_called_once_with("id", ["h1"])
    assert ("eq", ("user_id", "u1")) in calls["filters"]
    assert "resolved_at" in calls["payload"]


def test_an_unidentifiable_url_closes_nothing():
    with patch.object(hb, "fetch_paged") as read:
        assert hb.resolve_for_posting("u1", "") == 0
    read.assert_not_called()
