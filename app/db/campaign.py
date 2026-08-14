"""Операции с campaign_states в Supabase."""

from datetime import UTC, datetime

from app.db.client import get_supabase

# Heartbeat TTL (ZOMBIE_FIX_PLAN.md): the extension pings /extension/ping every 60s
# whenever it is loaded, and that ping stamps campaign_states.last_ping_at. A campaign
# whose flag says running but whose extension hasn't pinged within the TTL is a ZOMBIE
# (laptop closed / browser quit / crash / offline) — report it as not running and lazily
# flip the row. 150s = 2.5× the ping period: tolerates two missed pings + jitter.
HEARTBEAT_TTL_SECS = 150

# Grace after started_at during which we trust the flag without any campaign heartbeat.
# /campaign/start flips the flag, but the extension needs a moment to pick the campaign
# up — reaping inside this window would kill campaigns at birth. Two ping periods.
STARTUP_GRACE_SECS = 120


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def is_effectively_running(state: dict) -> bool:
    """The authoritative liveness check: the stored flag AND a fresh heartbeat.

    last_ping_at is None means no campaign heartbeat ever landed (pre-migration row, or
    the extension never confirmed it was running). Trusting the flag FOREVER in that case
    made zombies immortal, so it is trusted only inside STARTUP_GRACE of started_at. With
    no started_at either we know nothing and keep the legacy behaviour — trust the flag.
    """
    if not state.get("running"):
        return False
    now = datetime.now(UTC)
    lp = _parse_ts(state.get("last_ping_at"))
    if lp is None:
        started = _parse_ts(state.get("started_at"))
        if started is None:
            return True
        return (now - started).total_seconds() < STARTUP_GRACE_SECS
    return (now - lp).total_seconds() < HEARTBEAT_TTL_SECS


def build_readiness(profile: dict, running: bool, tier: str, submit_mode: str,
                    free_used: int | None, free_limit: int) -> dict:
    """Single source of truth for "can a campaign start MEANINGFULLY?" (pure — testable).

    Born from live confusion (2026-07-16): a fresh user with the extension connected but
    NO keywords could press Start — the button 'worked' but the campaign had nothing to
    do. Every start precondition the SERVER can know lives here; the dashboard renders
    the failed checks as a what's-left checklist instead of a dead button. (Extension
    installed/connected is a CLIENT-side check — the dashboard adds it via the PING
    bridge; the server can't see it.)
    """
    platforms = profile.get("platforms") or []
    ats_selected = any(p in ("greenhouse", "lever") for p in platforms)
    checks: list[dict] = []

    def add(check_id: str, ok, reason: str, fix: str) -> None:
        checks.append({
            "id": check_id,
            "ok": bool(ok),
            "reason": None if ok else reason,
            "fix": None if ok else fix,
        })

    add("onboarding", profile.get("onboarding_completed") is True,
        "Finish your profile setup first", "onboarding")
    add("keywords", bool(profile.get("keywords")),
        "Add at least one keyword — the campaign needs something to search for", "keywords")
    add("resume", (not ats_selected) or bool(profile.get("resume_url")),
        "Upload a resume — company-site (Greenhouse/Lever) applications require one", "settings")
    add("lever_tap", not ("lever" in platforms and submit_mode != "tap"),
        "Lever needs Tap mode (its captcha requires a human) — switch to Tap or unselect Lever", "tap")
    if tier == "free":
        add("free_quota", (free_used or 0) < free_limit,
            f"You've used all {free_limit} free applications — subscribe to keep applying", "upgrade")
    add("not_running", not running, "A campaign is already running", "campaign")

    return {
        "ready": all(c["ok"] for c in checks),
        "checks": checks,
        "tier": tier,
        "submit_mode": submit_mode,
    }


def get_state(user_id: str) -> dict:
    res = get_supabase().table("campaign_states").select("*").eq("user_id", user_id).execute()
    if res.data:
        s = res.data[0]
        return {
            "running": s.get("running", False),
            "filters": s.get("filters") or {},
            "started_at": s.get("started_at"),
            "last_ping_at": s.get("last_ping_at"),
        }
    return {"running": False, "filters": {}, "started_at": None, "last_ping_at": None}


def get_effective_state(user_id: str) -> dict:
    """get_state + zombie self-healing: if the flag is up but the heartbeat is stale,
    flip the row to not-running (lazy cleanup on read) and report not running. Keeps
    filters/started_at intact for forensics — only the flag is corrected."""
    state = get_state(user_id)
    if state["running"] and not is_effectively_running(state):
        try:
            (
                get_supabase()
                .table("campaign_states")
                .update({"running": False})
                .eq("user_id", user_id)
                .execute()
            )
        except Exception:
            pass  # cleanup is best-effort; the caller still gets running=False
        state["running"] = False
    return state


def reconcile_not_running(user_id: str) -> bool:
    """The extension reports it is NOT running a campaign — clear a flag that says it is.

    Only the extension can actually run a campaign, so its own view is authoritative in
    this direction (never the reverse: the dashboard must not be able to raise the flag
    from a client claim). Skipped inside STARTUP_GRACE of started_at so a ping racing
    /campaign/start can't reap a campaign the extension hasn't picked up yet.

    Returns True if a zombie was cleared. Only the flag is touched — filters/started_at
    stay for forensics, same as get_effective_state().
    """
    state = get_state(user_id)
    if not state["running"]:
        return False
    started = _parse_ts(state.get("started_at"))
    if started and (datetime.now(UTC) - started).total_seconds() < STARTUP_GRACE_SECS:
        return False
    try:
        (
            get_supabase()
            .table("campaign_states")
            .update({"running": False})
            .eq("user_id", user_id)
            .execute()
        )
    except Exception:
        return False
    return True


def touch_ping(user_id: str) -> None:
    """Stamp the extension heartbeat. Best-effort + update-only (never creates a row):
    safe to call before the last_ping_at migration has been applied."""
    try:
        (
            get_supabase()
            .table("campaign_states")
            .update({"last_ping_at": datetime.now(UTC).isoformat()})
            .eq("user_id", user_id)
            .execute()
        )
    except Exception:
        pass


def start(user_id: str, filters: dict) -> dict:
    now = datetime.now(UTC).isoformat()
    (
        get_supabase()
        .table("campaign_states")
        .upsert(
            {
                "user_id": user_id,
                "running": True,
                "filters": filters,
                "started_at": now,
            },
            on_conflict="user_id",
        )
        .execute()
    )
    # Freshly-started campaigns must not be instantly reaped: the first extension ping
    # can be up to 60s away, so stamp the heartbeat at start too (best-effort, separate
    # call so a missing column can't break start()).
    touch_ping(user_id)
    return {"running": True, "filters": filters, "started_at": now}


def stop(user_id: str) -> None:
    (
        get_supabase()
        .table("campaign_states")
        .upsert(
            {
                "user_id": user_id,
                "running": False,
                "filters": {},
                "started_at": None,
            },
            on_conflict="user_id",
        )
        .execute()
    )
