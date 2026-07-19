"""Операции с campaign_states в Supabase."""

from datetime import UTC, datetime

from app.db.client import get_supabase

# Heartbeat TTL (ZOMBIE_FIX_PLAN.md): the extension pings /extension/ping every 60s
# whenever it is loaded, and that ping stamps campaign_states.last_ping_at. A campaign
# whose flag says running but whose extension hasn't pinged within the TTL is a ZOMBIE
# (laptop closed / browser quit / crash / offline) — report it as not running and lazily
# flip the row. 150s = 2.5× the ping period: tolerates two missed pings + jitter.
HEARTBEAT_TTL_SECS = 150


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

    last_ping_at is None for rows from before the migration (or if the column doesn't
    exist yet) — keep the legacy behaviour then (trust the flag), so this deploys safely
    ahead of the migration and TTL activates the moment pings start stamping.
    """
    if not state.get("running"):
        return False
    lp = _parse_ts(state.get("last_ping_at"))
    if lp is None:
        return True
    return (datetime.now(UTC) - lp).total_seconds() < HEARTBEAT_TTL_SECS


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
