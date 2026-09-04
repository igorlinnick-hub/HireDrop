"""Stall watch — the detector for "campaign says running, but applications aren't growing".

Every heartbeat bug so far was found by Igor staring at the dashboard, and the zombie of
2026-08-12 ran for two days before anyone noticed (STATUS.md, "Observability нет вообще").
The heartbeat only answers *is the extension alive*. It says nothing about whether the run
is PRODUCING — and the failure mode that actually costs users their day is the silent one:
extension pinging happily, form filler stuck on a widget, zero applications for an hour.

So this measures the only output that matters — the timestamp of the last application —
and shouts when it stops moving:

  * a warn line in the user's own activity log (visible in the dashboard feed), and
  * an email to ALERT_EMAIL via Resend (ops), if configured.

Deliberately NOT a reaper: it never flips `running`. Killing campaigns from a second place
is exactly how the heartbeat got broken three times (#98/#107/#111) — one writer, one
decision. This one only reports.
"""

import asyncio
import random
import sys
from datetime import UTC, datetime

from app.db import activity as activity_db
from app.db import applications as apps_db
from app.db import campaign as campaign_db
from app.db.subscriptions import (
    daily_limit,
    get_free_apps_used,
    get_submit_mode,
    get_tier,
)
from config import (
    ALERT_EMAIL,
    FREE_APP_LIMIT,
    FRONTEND_URL,
    STALL_FIRST_MINUTES,
    STALL_GAP_MINUTES,
    STALL_WATCH_INTERVAL_SECS,
)

# Activity phase written by this module. Doubles as the dedup key: one alert per stall
# episode, not one per sweep (see scan()).
PHASE = "stall-watch"

STALL_FIRST_SECS = STALL_FIRST_MINUTES * 60
STALL_GAP_SECS = STALL_GAP_MINUTES * 60


def evaluate(
    state: dict,
    *,
    last_applied_at: str | None = None,
    submit_mode: str = "auto",
    today_count: int = 0,
    cap: int | None = None,
    free_used: int | None = None,
    free_limit: int | None = None,
    now: datetime | None = None,
    first_secs: int = STALL_FIRST_SECS,
    gap_secs: int = STALL_GAP_SECS,
) -> dict:
    """Pure verdict on one campaign: is it running-but-not-producing?

    Returns {"stalled", "reason", "silent_secs", "anchor"}. `anchor` is the last moment we
    know the run produced something — the campaign start before the first application, the
    application itself after that — and it is also the dedup window for the alert.

    Silence is only a symptom; these cases explain it away and must NOT alert:
      not_running      — flag down or heartbeat stale: that's the zombie path, already
                         handled (and self-healed) by campaign_db.get_effective_state.
      tap_mode         — in Tap the human submits; applications grow when the USER taps,
                         so "nothing applied" is a statement about the user, not the run.
      cap_reached /
      free_quota_spent — the run has nothing left to do. (A campaign that keeps running
                         after its cap is its own bug — not this detector's alert.)
      no_anchor        — pre-migration row with no started_at and no application ever:
                         nothing to measure from, so stay quiet rather than guess.
    """
    now = now or datetime.now(UTC)

    def verdict(reason: str, *, stalled: bool = False, silent: float = 0.0, anchor=None) -> dict:
        return {
            "stalled": stalled,
            "reason": reason,
            "silent_secs": int(silent),
            "anchor": anchor.isoformat() if isinstance(anchor, datetime) else anchor,
        }

    if not campaign_db.is_effectively_running(state):
        return verdict("not_running")
    if submit_mode == "tap":
        return verdict("tap_mode")
    if cap is not None and today_count >= cap:
        return verdict("cap_reached")
    if free_limit is not None and free_used is not None and free_used >= free_limit:
        return verdict("free_quota_spent")

    started = campaign_db.parse_ts(state.get("started_at"))
    applied = campaign_db.parse_ts(last_applied_at)
    # An application from a PREVIOUS run is older than started_at — max() picks the right
    # anchor either way, and tells us which threshold applies.
    after_first = applied is not None and (started is None or applied > started)
    anchor = applied if after_first else started
    if anchor is None:
        return verdict("no_anchor")

    silent = (now - anchor).total_seconds()
    threshold = gap_secs if after_first else first_secs
    if silent >= threshold:
        return verdict(
            "applications_stopped" if after_first else "no_first_application",
            stalled=True,
            silent=silent,
            anchor=anchor,
        )
    return verdict("healthy", silent=silent, anchor=anchor)


def judge_user(user_id: str, state: dict, *, now: datetime | None = None) -> dict:
    """evaluate() + the per-user reads it needs (mode, tier, caps, last application)."""
    submit_mode = get_submit_mode(user_id)
    tier = get_tier(user_id)
    free = tier == "free"
    return evaluate(
        state,
        last_applied_at=apps_db.last_applied_at(user_id),
        submit_mode=submit_mode,
        today_count=apps_db.count_today(user_id),
        cap=daily_limit(tier, submit_mode),
        free_used=get_free_apps_used(user_id) if free else None,
        free_limit=FREE_APP_LIMIT if free else None,
        now=now,
    )


def _minutes(secs: int) -> int:
    return max(1, round(secs / 60))


def alert_html(user_id: str, verdict: dict, recent: list[dict]) -> str:
    """Ops email body. The last log lines are the whole point — an alert that only says
    "stalled" sends you to the dashboard anyway; this one carries the evidence."""
    rows = (
        "".join(
            f'<tr><td style="padding:4px 10px 4px 0;color:#9b9bb0;white-space:nowrap;">'
            f"{(r.get('timestamp') or '')[:19]}</td>"
            f'<td style="padding:4px 0;color:#1a1a2e;">{(r.get("message") or "")[:300]}</td></tr>'
            for r in recent
        )
        or '<tr><td colspan="2" style="color:#9b9bb0;">— no activity lines at all —</td></tr>'
    )
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f3ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="max-width:640px;margin:32px auto;background:#fff;border-radius:14px;border:1px solid #e9e6f5;padding:28px;">
    <h1 style="font-size:18px;color:#1a1a2e;margin:0 0 6px;">Campaign running, nothing applied</h1>
    <p style="font-size:14px;color:#6b6b8a;line-height:1.6;margin:0 0 18px;">
      User <code>{user_id}</code> has a live campaign (heartbeat fresh) that has not produced
      an application in <b>{_minutes(verdict["silent_secs"])} min</b>.<br>
      Reason: <code>{verdict["reason"]}</code> &middot; measured from <code>{verdict["anchor"]}</code>
    </p>
    <table style="width:100%;border-collapse:collapse;font-size:12px;">{rows}</table>
    <p style="font-size:12px;color:#9b9bb0;margin:20px 0 0;">
      HireDrop stall watch &middot; <a href="{FRONTEND_URL}/dashboard" style="color:#6c5ce7;">dashboard</a>
    </p>
  </div>
</body>
</html>
"""


def raise_alert(user_id: str, verdict: dict, *, send: bool = True) -> None:
    """Write the user-visible warn line, then email ops.

    Order is deliberate: the activity line is also the dedup marker, so writing it first
    means a broken Resend key costs us one missed email — not an alert every sweep for as
    long as the stall lasts. Send failures already log `[CRITICAL email]` to stderr.
    """
    mins = _minutes(verdict["silent_secs"])
    activity_db.write(
        user_id,
        f"⚠️ No application submitted in the last {mins} min while the campaign is running "
        "— the run may be stuck (form filler waiting, or every job skipped).",
        level="warn",
        phase=PHASE,
    )
    print(
        f"[stall-watch] user={user_id} reason={verdict['reason']} silent={mins}m "
        f"anchor={verdict['anchor']}",
        file=sys.stderr,
    )
    if not (send and ALERT_EMAIL):
        return
    from modules.email_sender import send_email

    recent = []
    try:
        recent = activity_db.list_recent(user_id, limit=8)
    except Exception as exc:  # noqa: BLE001 — an alert without context still beats no alert
        print(f"[stall-watch] activity read failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    send_email(
        ALERT_EMAIL,
        f"[HireDrop] Campaign stalled — no application in {mins}m ({user_id[:8]})",
        alert_html(user_id, verdict, recent),
    )


def report(*, now: datetime | None = None) -> list[dict]:
    """Read-only verdict for every running campaign — no writes, no email.

    The sweep consumes it, and the admin endpoint exposes it, so "is the detector seeing
    what I see?" is answerable on demand instead of only when an alert happens to fire.
    """
    out: list[dict] = []
    try:
        states = campaign_db.list_running()
    except Exception as exc:  # noqa: BLE001 — a sweep that can't read must not kill the loop
        print(f"[stall-watch] list_running failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return out

    for state in states:
        user_id = state.get("user_id")
        if not user_id:
            continue
        try:
            verdict = judge_user(user_id, state, now=now)
        except Exception as exc:  # noqa: BLE001 — one bad user must not skip the rest
            print(
                f"[stall-watch] user={user_id} judge failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        out.append({"user_id": user_id, **verdict})
    return out


def scan(*, now: datetime | None = None, send: bool = True) -> list[dict]:
    """One sweep over every running campaign. Returns the stalls it alerted on.

    Dedup: an alert is skipped if a stall-watch line already exists since the anchor. The
    anchor moves forward the moment an application lands, so a run that recovers and stalls
    again alerts again — while a run that stays stuck alerts exactly once.
    """
    alerts: list[dict] = []
    for item in report(now=now):
        if not item["stalled"]:
            continue
        user_id = item["user_id"]
        try:
            if activity_db.has_since(user_id, PHASE, item["anchor"]):
                continue
            raise_alert(user_id, item, send=send)
        except Exception as exc:  # noqa: BLE001 — one failed alert must not skip the rest
            print(
                f"[stall-watch] user={user_id} alert failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        alerts.append(item)
    return alerts


async def watch_loop() -> None:
    """Background sweep, started from the app lifespan."""
    # Desynchronize the uvicorn workers (Procfile runs 2): without jitter both sweep in the
    # same second, both pass the dedup check, and ops gets the alert twice.
    await asyncio.sleep(random.uniform(0, min(60, STALL_WATCH_INTERVAL_SECS)))  # noqa: S311
    while True:
        try:
            # Supabase calls are blocking HTTP. Off the event loop — the email poller doing
            # exactly this on the loop is the reason it was disabled (see main.py lifespan).
            await asyncio.to_thread(scan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[stall-watch] sweep error: {type(exc).__name__}: {exc}", file=sys.stderr)
        await asyncio.sleep(STALL_WATCH_INTERVAL_SECS)
