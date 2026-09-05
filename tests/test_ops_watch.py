"""Ops watch tests — 5xx burst detection and scraper silent-zero streaks.

Pure classes are tested with injected clocks (no sleeps). The middleware hook is
tested through the app: an endpoint 5xx must land in the watch.
"""

from datetime import UTC, datetime, timedelta

from app.ops_watch import FiveXXWatch, ScrapeWatch

T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------- FiveXXWatch


def test_5xx_below_threshold_is_silent():
    w = FiveXXWatch(threshold=3, window_minutes=10, cooldown_minutes=30)
    assert w.record("/a", 500, now=T0) is None
    assert w.record("/b", 502, now=T0 + timedelta(minutes=1)) is None


def test_5xx_burst_alerts_once_then_cooldown():
    w = FiveXXWatch(threshold=3, window_minutes=10, cooldown_minutes=30)
    w.record("/a", 500, now=T0)
    w.record("/b", 500, now=T0 + timedelta(minutes=1))
    alert = w.record("/c", 503, now=T0 + timedelta(minutes=2))
    assert alert is not None
    assert alert["kind"] == "5xx_burst"
    assert alert["count"] == 3
    # Still bursting inside the cooldown → no second alert.
    assert w.record("/d", 500, now=T0 + timedelta(minutes=3)) is None
    # A fresh burst after the cooldown (3 errors inside the window again) alerts again.
    w.record("/e", 500, now=T0 + timedelta(minutes=31))
    w.record("/f", 500, now=T0 + timedelta(minutes=32))
    assert w.record("/g", 500, now=T0 + timedelta(minutes=33)) is not None


def test_5xx_old_events_fall_out_of_window():
    w = FiveXXWatch(threshold=3, window_minutes=10, cooldown_minutes=30)
    w.record("/a", 500, now=T0)
    w.record("/b", 500, now=T0 + timedelta(minutes=1))
    # Third error arrives 20 min later — the first two are out of the window.
    assert w.record("/c", 500, now=T0 + timedelta(minutes=21)) is None


def test_5xx_snapshot_counts_recent_only():
    w = FiveXXWatch(threshold=5, window_minutes=10, cooldown_minutes=30)
    w.record("/old", 500, now=T0)
    w.record("/new", 500, now=T0 + timedelta(minutes=15))
    snap = w.snapshot(now=T0 + timedelta(minutes=16))
    assert snap["recent_5xx"] == 1
    assert snap["sample"][-1]["path"] == "/new"


# ---------------------------------------------------------------- ScrapeWatch


def test_scrape_nonzero_resets_streak():
    w = ScrapeWatch(streak_threshold=3, cooldown_minutes=30)
    assert w.record("remoteok", 0, now=T0) is None
    assert w.record("remoteok", 0, now=T0) is None
    assert w.record("remoteok", 7, now=T0) is None  # reset
    assert w.record("remoteok", 0, now=T0) is None
    assert w.record("remoteok", 0, now=T0) is None
    alert = w.record("remoteok", 0, now=T0)
    assert alert is not None
    assert alert["kind"] == "scraper_silent_zero"
    assert alert["platform"] == "remoteok"
    assert alert["zero_streak"] == 3


def test_scrape_streaks_are_per_platform():
    w = ScrapeWatch(streak_threshold=2, cooldown_minutes=30)
    w.record("remoteok", 0, now=T0)
    assert w.record("indeed", 0, now=T0) is None  # indeed streak = 1, not 2
    alert = w.record("remoteok", 0, now=T0)
    assert alert is not None and alert["platform"] == "remoteok"


def test_scrape_alert_respects_cooldown():
    w = ScrapeWatch(streak_threshold=2, cooldown_minutes=30)
    w.record("remoteok", 0, now=T0)
    assert w.record("remoteok", 0, now=T0 + timedelta(minutes=1)) is not None
    # Streak keeps growing but the cooldown gags repeat alerts…
    assert w.record("remoteok", 0, now=T0 + timedelta(minutes=2)) is None
    # …until it expires.
    assert w.record("remoteok", 0, now=T0 + timedelta(minutes=40)) is not None


def test_scrape_snapshot_shape():
    w = ScrapeWatch(streak_threshold=5, cooldown_minutes=30)
    w.record("remoteok", 3, now=T0)
    w.record("remoteok", 0, now=T0 + timedelta(minutes=5))
    snap = w.snapshot()
    assert snap["remoteok"]["zero_streak"] == 1
    assert snap["remoteok"]["total_scrapes"] == 2
    assert snap["remoteok"]["last_count"] == 0
    assert snap["remoteok"]["last_nonzero_at"] == T0.isoformat()


# ---------------------------------------------------------------- wiring


def test_middleware_records_explicit_5xx(auth_client):
    """A 5xx response must land in the ops watch via the timing middleware.
    /billing/checkout with Stripe unconfigured is a stable in-app 503 source."""
    from app.ops_watch import five_xx

    before = len(five_xx.events)
    r = auth_client.post("/api/v1/billing/checkout", json={"plan": "weekly"})
    assert r.status_code == 503
    assert len(five_xx.events) == before + 1
    assert five_xx.events[-1][1] == "/api/v1/billing/checkout"


def test_ops_scan_requires_admin(auth_client):
    r = auth_client.get("/api/v1/tools/ops-scan")
    assert r.status_code == 403
