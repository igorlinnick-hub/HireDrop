"""A repeat wall is NEW news: _update_open re-stamps created_at (web freshness reads it
as "when the latest wall happened"; the DB default fires on INSERT only)."""

from unittest.mock import MagicMock, patch

from app.db import handbacks


def test_update_open_restamps_created_at():
    sb = MagicMock()
    chain = (
        sb.table.return_value.update.return_value.eq.return_value.eq.return_value.is_.return_value
    )
    chain.execute.return_value.data = [{"id": "x"}]
    with patch.object(handbacks, "get_supabase", return_value=sb):
        out = handbacks._update_open("u1", {"url": "https://ex.com/j", "reason": "wall"})
    assert out == {"id": "x"}
    sent = sb.table.return_value.update.call_args[0][0]
    assert sent["reason"] == "wall"
    assert "created_at" in sent and sent["created_at"].startswith("20")


def test_insert_path_sends_no_created_at():
    """A fresh row keeps the DB default — only the UPDATE path re-stamps."""
    sb = MagicMock()
    up = sb.table.return_value.update.return_value.eq.return_value.eq.return_value.is_.return_value
    up.execute.return_value.data = []  # no open row -> insert
    ins = sb.table.return_value.insert.return_value
    ins.execute.return_value.data = [{"id": "y"}]
    with patch.object(handbacks, "get_supabase", return_value=sb):
        out = handbacks._add_row("u1", {"url": "https://ex.com/j2", "reason": "wall"})
    assert out == {"id": "y"}
    sent = sb.table.return_value.insert.call_args[0][0]
    assert "created_at" not in sent
