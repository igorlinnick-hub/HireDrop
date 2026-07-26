"""Heartbeat-TTL truth table (ZOMBIE_FIX_PLAN.md): a campaign is effectively running only
if the flag is up AND the extension pinged within HEARTBEAT_TTL_SECS. Pre-migration rows
(last_ping_at absent/None) keep legacy behaviour — trust the flag."""

from datetime import UTC, datetime, timedelta

from app.db.campaign import HEARTBEAT_TTL_SECS, is_effectively_running


def _iso(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def test_not_running_is_never_effective():
    assert is_effectively_running({"running": False, "last_ping_at": _iso(0)}) is False
    assert is_effectively_running({"running": False, "last_ping_at": None}) is False


def test_running_with_fresh_ping_is_effective():
    assert is_effectively_running({"running": True, "last_ping_at": _iso(5)}) is True
    # Just inside the TTL
    assert is_effectively_running({"running": True, "last_ping_at": _iso(HEARTBEAT_TTL_SECS - 5)}) is True


def test_running_with_stale_ping_is_a_zombie():
    assert is_effectively_running({"running": True, "last_ping_at": _iso(HEARTBEAT_TTL_SECS + 5)}) is False
    assert is_effectively_running({"running": True, "last_ping_at": _iso(3600)}) is False


def test_pre_migration_rows_trust_the_flag():
    # last_ping_at missing or None → legacy behaviour (never expire), so the backend
    # can deploy BEFORE the column migration without flipping live campaigns.
    assert is_effectively_running({"running": True}) is True
    assert is_effectively_running({"running": True, "last_ping_at": None}) is True


def test_garbage_timestamp_trusts_the_flag():
    assert is_effectively_running({"running": True, "last_ping_at": "not-a-date"}) is True


def test_z_suffix_timestamps_parse():
    z = (datetime.now(UTC) - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert is_effectively_running({"running": True, "last_ping_at": z}) is True
