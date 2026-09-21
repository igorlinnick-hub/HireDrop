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

from app.db import handbacks as hb


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
    client, calls, _tbl = _fake_supabase()
    with patch.object(hb, "get_supabase", return_value=client):
        hb.add("u1", {"job_title": "Assoc. Director", "url": "https://x/form"})

    assert calls["conflict"] == "user_id,url"
    assert calls["payload"]["user_id"] == "u1"


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
