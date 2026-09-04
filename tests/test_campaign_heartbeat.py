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
    assert (
        is_effectively_running({"running": True, "last_ping_at": _iso(HEARTBEAT_TTL_SECS - 5)})
        is True
    )


def test_running_with_stale_ping_is_a_zombie():
    assert (
        is_effectively_running({"running": True, "last_ping_at": _iso(HEARTBEAT_TTL_SECS + 5)})
        is False
    )
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


# --- The hole that let a real zombie survive 2 days (Igor, 2026-08-14) -------------
# The TTL was fed by EVERY extension ping, including idle ones, so a loaded-but-idle
# extension kept last_ping_at fresh forever and a `running` flag from days earlier
# still read as live. Only a ping that says the campaign is running is its heartbeat.

from unittest.mock import patch  # noqa: E402

from app.db.campaign import STARTUP_GRACE_SECS, reconcile_not_running  # noqa: E402


def _state_rows(supabase_mock, row):
    supabase_mock.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        row
    ]


def test_never_heartbeated_campaign_is_not_immortal():
    """No ping ever landed and the campaign started long ago — it cannot still be live."""
    assert (
        is_effectively_running({"running": True, "last_ping_at": None, "started_at": _iso(3600)})
        is False
    )


def test_never_heartbeated_campaign_is_trusted_inside_startup_grace():
    """The extension can be up to a ping period away from confirming — don't reap at birth."""
    assert (
        is_effectively_running({"running": True, "last_ping_at": None, "started_at": _iso(10)})
        is True
    )
    assert (
        is_effectively_running(
            {"running": True, "last_ping_at": None, "started_at": _iso(STARTUP_GRACE_SECS + 5)}
        )
        is False
    )


def test_reconcile_clears_the_flag_when_the_extension_says_it_is_idle(supabase_mock):
    _state_rows(supabase_mock, {"running": True, "started_at": _iso(3600), "last_ping_at": None})

    assert reconcile_not_running("u1") is True

    update = supabase_mock.table.return_value.update
    update.assert_called_once_with({"running": False})


def test_reconcile_respects_the_startup_grace(supabase_mock):
    """A ping racing /campaign/start must not kill the campaign it just started."""
    _state_rows(supabase_mock, {"running": True, "started_at": _iso(5), "last_ping_at": None})

    assert reconcile_not_running("u1") is False
    supabase_mock.table.return_value.update.assert_not_called()


def test_reconcile_is_a_noop_when_no_campaign_is_flagged(supabase_mock):
    _state_rows(supabase_mock, {"running": False, "started_at": None, "last_ping_at": None})

    assert reconcile_not_running("u1") is False
    supabase_mock.table.return_value.update.assert_not_called()


def test_idle_ping_does_not_refresh_the_heartbeat(auth_client):
    """THE regression guard: an idle extension must never keep a zombie alive."""
    with (
        patch("app.routers.campaign.campaign_db.touch_ping") as touch,
        patch("app.routers.campaign.campaign_db.reconcile_not_running") as reconcile,
    ):
        res = auth_client.post("/api/v1/extension/ping", json={"campaign_running": False})

    assert res.status_code == 200
    touch.assert_not_called()
    reconcile.assert_called_once()


def test_running_ping_stamps_the_heartbeat(auth_client):
    with (
        patch("app.routers.campaign.campaign_db.touch_ping") as touch,
        patch("app.routers.campaign.campaign_db.reconcile_not_running") as reconcile,
    ):
        res = auth_client.post("/api/v1/extension/ping", json={"campaign_running": True})

    assert res.status_code == 200
    touch.assert_called_once()
    reconcile.assert_not_called()


def test_campaign_activity_line_refreshes_the_heartbeat(auth_client):
    """An extension activity line written DURING a running campaign is a heartbeat.

    The alarm-only heartbeat is not enough: chrome.alarms is throttled when the machine
    idles, so a live run blew the TTL and was reaped mid-application (08-15). These lines
    are only written while a campaign runs, so they still prove the CAMPAIGN is alive —
    the property ZOMBIE_FIX_PLAN.md requires — not merely that the extension is loaded.
    """
    with (
        patch("app.routers.activity.campaign_db.get_state", return_value={"running": True}),
        patch("app.routers.activity.campaign_db.touch_ping") as touch,
        patch("app.routers.activity.activity_db.write", return_value="id1"),
    ):
        res = auth_client.post(
            "/api/v1/activity", json={"message": "Applied: x @ y", "phase": "extension"}
        )

    assert res.status_code == 200
    touch.assert_called_once()


def test_activity_line_without_a_campaign_is_not_a_heartbeat(auth_client):
    with (
        patch("app.routers.activity.campaign_db.get_state", return_value={"running": False}),
        patch("app.routers.activity.campaign_db.touch_ping") as touch,
        patch("app.routers.activity.activity_db.write", return_value="id1"),
    ):
        res = auth_client.post("/api/v1/activity", json={"message": "hello", "phase": "extension"})

    assert res.status_code == 200
    touch.assert_not_called()


def test_non_extension_activity_never_stamps_the_heartbeat(auth_client):
    """Dashboard/server-written lines must not keep a dead campaign alive."""
    with (
        patch("app.routers.activity.campaign_db.get_state", return_value={"running": True}),
        patch("app.routers.activity.campaign_db.touch_ping") as touch,
        patch("app.routers.activity.activity_db.write", return_value="id1"),
    ):
        res = auth_client.post("/api/v1/activity", json={"message": "hello", "phase": "dashboard"})

    assert res.status_code == 200
    touch.assert_not_called()


def test_false_ping_cannot_reap_a_campaign_with_a_live_heartbeat(supabase_mock):
    """One browser can hold two installs of the extension. The idle one pings
    "campaign_running: false" every minute under the same account, and on 08-15 that
    reaped a campaign the OTHER install was actively filling a form for — three runs in a
    row, storage forensics proving the running instance never lowered its own flag.
    A claim of "not running" must therefore never outrank a live heartbeat."""
    _state_rows(
        supabase_mock,
        {"running": True, "started_at": _iso(3600), "last_ping_at": _iso(10)},
    )

    assert reconcile_not_running("u1") is False
    supabase_mock.table.return_value.update.assert_not_called()


def test_false_ping_still_reaps_a_silent_campaign(supabase_mock):
    """The zombie guard itself stays intact: no heartbeat, no mercy."""
    _state_rows(
        supabase_mock,
        {"running": True, "started_at": _iso(3600), "last_ping_at": _iso(HEARTBEAT_TTL_SECS + 30)},
    )

    assert reconcile_not_running("u1") is True
