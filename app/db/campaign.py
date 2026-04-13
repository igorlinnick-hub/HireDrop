"""Операции с campaign_states в Supabase."""
from datetime import datetime, timezone
from app.db.client import get_supabase


def get_state(user_id: str) -> dict:
    res = (
        get_supabase()
        .table("campaign_states")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )
    if res.data:
        s = res.data[0]
        return {
            "running": s.get("running", False),
            "filters": s.get("filters") or {},
            "started_at": s.get("started_at"),
        }
    return {"running": False, "filters": {}, "started_at": None}


def start(user_id: str, filters: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
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
