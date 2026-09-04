"""Stall watch: "campaign running, but applications aren't growing" (app/stall_watch.py).

The heartbeat proves the extension is alive; it says nothing about the run producing.
These pin the verdict table — especially the silences that must NOT alert, since a
detector that cries wolf on Tap mode or a spent daily cap gets muted within a week.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from app import stall_watch


def _iso(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


def _live(started_secs_ago: float = 3600) -> dict:
    """A campaign whose flag is up and whose heartbeat is fresh."""
    return {"running": True, "started_at": _iso(started_secs_ago), "last_ping_at": _iso(10)}


# --- silences with an explanation: never alert ------------------------------------


def test_zombie_is_not_a_stall():
    # Flag up, heartbeat stale — that's the zombie path, owned by get_effective_state.
    state = {"running": True, "started_at": _iso(7200), "last_ping_at": _iso(7000)}
    assert stall_watch.evaluate(state)["reason"] == "not_running"
    assert stall_watch.evaluate({"running": False})["stalled"] is False


def test_tap_mode_is_not_a_stall():
    # In Tap the human submits; zero applications describes the user, not the run.
    v = stall_watch.evaluate(_live(), submit_mode="tap")
    assert (v["stalled"], v["reason"]) == (False, "tap_mode")


def test_daily_cap_reached_is_not_a_stall():
    v = stall_watch.evaluate(_live(), today_count=30, cap=30)
    assert (v["stalled"], v["reason"]) == (False, "cap_reached")


def test_spent_free_quota_is_not_a_stall():
    v = stall_watch.evaluate(_live(), free_used=40, free_limit=40)
    assert (v["stalled"], v["reason"]) == (False, "free_quota_spent")


def test_no_anchor_stays_quiet():
    # Pre-migration row: no started_at, never applied — nothing to measure from.
    state = {"running": True, "started_at": None, "last_ping_at": _iso(10)}
    v = stall_watch.evaluate(state, last_applied_at=None)
    assert (v["stalled"], v["reason"]) == (False, "no_anchor")


# --- the real thing --------------------------------------------------------------


def test_first_application_gets_the_longer_grace():
    # Discovery + scoring + a cover letter make the FIRST application legitimately slow.
    inside = stall_watch.evaluate(_live(stall_watch.STALL_FIRST_SECS - 120))
    assert (inside["stalled"], inside["reason"]) == (False, "healthy")

    over = stall_watch.evaluate(_live(stall_watch.STALL_FIRST_SECS + 120))
    assert over["stalled"] is True
    assert over["reason"] == "no_first_application"
    assert over["silent_secs"] >= stall_watch.STALL_FIRST_SECS


def test_applications_that_stop_mid_run_alert_on_the_gap_threshold():
    state = _live(started_secs_ago=6 * 3600)
    fresh = stall_watch.evaluate(state, last_applied_at=_iso(stall_watch.STALL_GAP_SECS - 120))
    assert (fresh["stalled"], fresh["reason"]) == (False, "healthy")

    stuck_at = _iso(stall_watch.STALL_GAP_SECS + 120)
    stuck = stall_watch.evaluate(state, last_applied_at=stuck_at)
    assert stuck["stalled"] is True
    assert stuck["reason"] == "applications_stopped"
    # The anchor is the application itself — that's the dedup window for the alert.
    assert stuck["anchor"] == stuck_at


def test_application_from_a_previous_run_does_not_count_as_progress():
    # Applied yesterday, campaign started 50 min ago → still "no first application".
    state = _live(started_secs_ago=stall_watch.STALL_FIRST_SECS + 300)
    v = stall_watch.evaluate(state, last_applied_at=_iso(86400))
    assert (v["stalled"], v["reason"]) == (True, "no_first_application")
    assert v["anchor"] == state["started_at"]


def test_thresholds_are_overridable():
    v = stall_watch.evaluate(_live(700), first_secs=600)
    assert v["stalled"] is True


# --- sweep: dedup, isolation, email ------------------------------------------------

_STALLED = {
    "stalled": True,
    "reason": "no_first_application",
    "silent_secs": 3000,
    "anchor": _iso(3000),
}


def test_scan_alerts_once_per_stall_episode():
    with (
        patch.object(stall_watch.campaign_db, "list_running", return_value=[{"user_id": "u1"}]),
        patch.object(stall_watch, "judge_user", return_value=_STALLED),
        patch.object(stall_watch.activity_db, "has_since", return_value=False) as has_since,
        patch.object(stall_watch.activity_db, "write") as write,
    ):
        alerts = stall_watch.scan(send=False)
    assert [a["user_id"] for a in alerts] == ["u1"]
    assert write.call_count == 1
    # Dedup window is the anchor, not "the last hour" — a run that recovers can alert again.
    assert has_since.call_args.args[2] == _STALLED["anchor"]


def test_scan_skips_a_user_already_alerted_for_this_episode():
    with (
        patch.object(stall_watch.campaign_db, "list_running", return_value=[{"user_id": "u1"}]),
        patch.object(stall_watch, "judge_user", return_value=_STALLED),
        patch.object(stall_watch.activity_db, "has_since", return_value=True),
        patch.object(stall_watch.activity_db, "write") as write,
    ):
        assert stall_watch.scan(send=False) == []
    write.assert_not_called()


def test_one_broken_user_does_not_stop_the_sweep():
    def judge(user_id, state, now=None):
        if user_id == "boom":
            raise RuntimeError("supabase down")
        return _STALLED

    with (
        patch.object(
            stall_watch.campaign_db,
            "list_running",
            return_value=[{"user_id": "boom"}, {"user_id": "u2"}],
        ),
        patch.object(stall_watch, "judge_user", side_effect=judge),
        patch.object(stall_watch.activity_db, "has_since", return_value=False),
        patch.object(stall_watch.activity_db, "write"),
    ):
        alerts = stall_watch.scan(send=False)
    assert [a["user_id"] for a in alerts] == ["u2"]


def test_scan_survives_an_unreadable_campaign_table():
    with patch.object(stall_watch.campaign_db, "list_running", side_effect=RuntimeError("nope")):
        assert stall_watch.scan(send=False) == []


def test_alert_emails_ops_with_the_recent_log_lines():
    recent = [{"timestamp": "2026-09-03T10:00:00Z", "message": "🔍 Scanning ZipRecruiter"}]
    with (
        patch.object(stall_watch, "ALERT_EMAIL", "ops@hiredrop.io"),
        patch.object(stall_watch.activity_db, "write"),
        patch.object(stall_watch.activity_db, "list_recent", return_value=recent),
        patch("modules.email_sender.send_email", return_value=True) as send_email,
    ):
        stall_watch.raise_alert("user-1234-abcd", dict(_STALLED))
    to, subject, html = send_email.call_args.args
    assert to == "ops@hiredrop.io"
    assert "50m" in subject
    assert "Scanning ZipRecruiter" in html


def test_no_alert_email_configured_still_writes_the_activity_line():
    with (
        patch.object(stall_watch, "ALERT_EMAIL", ""),
        patch.object(stall_watch.activity_db, "write") as write,
        patch("modules.email_sender.send_email") as send_email,
    ):
        stall_watch.raise_alert("u1", dict(_STALLED))
    write.assert_called_once()
    send_email.assert_not_called()


# --- admin endpoint ----------------------------------------------------------------


def test_stall_scan_endpoint_is_admin_only(auth_client):
    with patch("app.routers.tools.is_admin", return_value=False):
        assert auth_client.get("/api/v1/tools/stall-scan").status_code == 403


def test_stall_scan_endpoint_reports_without_writing(auth_client):
    verdicts = [{"user_id": "u1", **_STALLED}, {"user_id": "u2", "stalled": False}]
    with (
        patch("app.routers.tools.is_admin", return_value=True),
        patch.object(stall_watch, "report", return_value=verdicts),
        patch.object(stall_watch.activity_db, "write") as write,
    ):
        res = auth_client.get("/api/v1/tools/stall-scan")
    assert res.status_code == 200
    body = res.json()
    assert body["running_campaigns"] == 2
    assert [v["user_id"] for v in body["stalled"]] == ["u1"]
    # Read-only: looking must never mark the user's log.
    write.assert_not_called()
