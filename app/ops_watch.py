"""Ops watch — the two blind spots stall_watch doesn't cover (STATUS.md "Известные дыры"):

  1. 5xx bursts. A single 500 is noise; a burst means a deploy broke something or a
     dependency (Supabase/Anthropic/Stripe) fell over — and today nobody notices until
     a user does.
  2. Silent-zero scrapers. A source that is KNOWN dead carries `unavailable_reason`
     (#130), but a live source that starts returning 0 on every call — the exact
     signature of the jobspy package mix-up (#113) and dead Google Jobs — looks
     identical to "no matching jobs" unless someone counts the streak.

Same philosophy as app/stall_watch.py: report, never act. Alerts go to stderr (Railway
log) always, and to ALERT_EMAIL via Resend when configured. State is in-memory and
per-worker (Procfile runs 2 uvicorn workers) — each worker watches its own traffic,
which at worst duplicates an email; a shared store isn't worth the moving parts yet.
"""

import sys
import threading
from collections import deque
from datetime import UTC, datetime, timedelta

from config import (
    ALERT_EMAIL,
    OPS_5XX_THRESHOLD,
    OPS_5XX_WINDOW_MINUTES,
    OPS_ALERT_COOLDOWN_MINUTES,
    OPS_ZERO_STREAK_THRESHOLD,
)


def _now() -> datetime:
    return datetime.now(UTC)


class FiveXXWatch:
    """Sliding-window counter over server errors. Pure logic — testable without the app."""

    def __init__(
        self,
        threshold: int = OPS_5XX_THRESHOLD,
        window_minutes: int = OPS_5XX_WINDOW_MINUTES,
        cooldown_minutes: int = OPS_ALERT_COOLDOWN_MINUTES,
    ):
        self.threshold = threshold
        self.window = timedelta(minutes=window_minutes)
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.events: deque[tuple[datetime, str, int]] = deque(maxlen=200)
        self.last_alert_at: datetime | None = None
        self._lock = threading.Lock()

    def record(self, path: str, status: int, now: datetime | None = None) -> dict | None:
        """Record one 5xx. Returns an alert dict when the burst threshold is crossed
        (respecting cooldown), else None."""
        now = now or _now()
        with self._lock:
            self.events.append((now, path, status))
            cutoff = now - self.window
            recent = [e for e in self.events if e[0] >= cutoff]
            if len(recent) < self.threshold:
                return None
            if self.last_alert_at and now - self.last_alert_at < self.cooldown:
                return None
            self.last_alert_at = now
            return {
                "kind": "5xx_burst",
                "count": len(recent),
                "window_minutes": int(self.window.total_seconds() // 60),
                "sample": [
                    {"at": t.isoformat(), "path": p, "status": s} for t, p, s in recent[-8:]
                ],
            }

    def snapshot(self, now: datetime | None = None) -> dict:
        now = now or _now()
        cutoff = now - self.window
        with self._lock:
            recent = [e for e in self.events if e[0] >= cutoff]
            return {
                "recent_5xx": len(recent),
                "threshold": self.threshold,
                "window_minutes": int(self.window.total_seconds() // 60),
                "last_alert_at": self.last_alert_at.isoformat() if self.last_alert_at else None,
                "sample": [
                    {"at": t.isoformat(), "path": p, "status": s} for t, p, s in recent[-8:]
                ],
            }


class ScrapeWatch:
    """Consecutive-zero streaks per platform. A platform that hits the streak threshold
    while it does NOT declare unavailable_reason is the #113 signature: alive on paper,
    contributing nothing."""

    def __init__(
        self,
        streak_threshold: int = OPS_ZERO_STREAK_THRESHOLD,
        cooldown_minutes: int = OPS_ALERT_COOLDOWN_MINUTES,
    ):
        self.streak_threshold = streak_threshold
        self.cooldown = timedelta(minutes=cooldown_minutes)
        # platform → {"zero_streak", "total", "last_count", "last_at", "last_nonzero_at",
        #             "last_alert_at"}
        self.platforms: dict[str, dict] = {}
        self._lock = threading.Lock()

    def record(self, platform: str, count: int, now: datetime | None = None) -> dict | None:
        """Record one scrape result. Returns an alert dict when a platform's zero-streak
        crosses the threshold (respecting cooldown), else None."""
        now = now or _now()
        with self._lock:
            st = self.platforms.setdefault(
                platform,
                {
                    "zero_streak": 0,
                    "total": 0,
                    "last_count": 0,
                    "last_at": None,
                    "last_nonzero_at": None,
                    "last_alert_at": None,
                },
            )
            st["total"] += 1
            st["last_count"] = count
            st["last_at"] = now
            if count > 0:
                st["zero_streak"] = 0
                st["last_nonzero_at"] = now
                return None
            st["zero_streak"] += 1
            if st["zero_streak"] < self.streak_threshold:
                return None
            if st["last_alert_at"] and now - st["last_alert_at"] < self.cooldown:
                return None
            st["last_alert_at"] = now
            return {
                "kind": "scraper_silent_zero",
                "platform": platform,
                "zero_streak": st["zero_streak"],
                "last_nonzero_at": (
                    st["last_nonzero_at"].isoformat() if st["last_nonzero_at"] else None
                ),
            }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                p: {
                    "zero_streak": st["zero_streak"],
                    "total_scrapes": st["total"],
                    "last_count": st["last_count"],
                    "last_at": st["last_at"].isoformat() if st["last_at"] else None,
                    "last_nonzero_at": (
                        st["last_nonzero_at"].isoformat() if st["last_nonzero_at"] else None
                    ),
                }
                for p, st in self.platforms.items()
            }


# Module-level singletons — one per worker process, wired from main.py / jobs.py.
five_xx = FiveXXWatch()
scrapes = ScrapeWatch()


def _alert_lines(alert: dict) -> tuple[str, str]:
    """(subject, plain summary) for an alert dict."""
    if alert["kind"] == "5xx_burst":
        subj = f"[HireDrop] {alert['count']} server errors in {alert['window_minutes']} min"
        body = "\n".join(f"{s['at']} {s['status']} {s['path']}" for s in alert["sample"])
    else:
        subj = (
            f"[HireDrop] scraper '{alert['platform']}' returned 0 jobs "
            f"{alert['zero_streak']} times in a row"
        )
        body = (
            f"platform={alert['platform']} zero_streak={alert['zero_streak']} "
            f"last_nonzero_at={alert['last_nonzero_at']}\n"
            "Silent-zero signature (#113/#130): alive on paper, contributing nothing. "
            "Check: .venv/bin/python scripts/audit_discovery_sources.py"
        )
    return subj, body


def emit(alert: dict) -> None:
    """stderr always; email off-thread when ALERT_EMAIL is set. Never raises — an
    observability failure must not break the request that triggered it."""
    try:
        subject, body = _alert_lines(alert)
        print(f"[ops-watch] {subject}\n{body}", file=sys.stderr)
        if not ALERT_EMAIL:
            return

        def _send():
            try:
                from modules.email_sender import send_email

                html = f"<pre style='font-size:13px'>{body}</pre>"
                send_email(ALERT_EMAIL, subject, html)
            except Exception as exc:  # noqa: BLE001
                print(f"[ops-watch] email failed: {type(exc).__name__}: {exc}", file=sys.stderr)

        threading.Thread(target=_send, daemon=True).start()
    except Exception as exc:  # noqa: BLE001
        print(f"[ops-watch] emit failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def record_5xx(path: str, status: int) -> None:
    alert = five_xx.record(path, status)
    if alert:
        emit(alert)


def record_scrape(platform: str, count: int) -> None:
    alert = scrapes.record(platform, count)
    if alert:
        emit(alert)


def report() -> dict:
    """Read-only state for the admin endpoint (per-worker view)."""
    return {"five_xx": five_xx.snapshot(), "scrapers": scrapes.snapshot()}
