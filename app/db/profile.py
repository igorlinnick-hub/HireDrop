"""Операции с таблицей profiles в Supabase."""
from app.db.client import get_supabase

_DEFAULTS = {
    "name": "",
    "last_name": "",
    "phone": "",
    "keywords": [],
    "location": "remote",
    "job_type": "full-time",
    "platforms": ["remoteok"],
    "writing_style": "",
    "resume_url": "",
    "onboarding_completed": False,
}


def get_profile(user_id: str) -> dict:
    res = (
        get_supabase()
        .table("profiles")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )
    if not res.data:
        return dict(_DEFAULTS)

    p = res.data[0]
    return {
        "name": p.get("name") or "",
        "last_name": p.get("last_name") or "",
        "phone": p.get("phone") or "",
        "keywords": p.get("keywords") or [],
        "location": p.get("location") or "remote",
        "job_type": p.get("job_type") or "full-time",
        "platforms": p.get("platforms") or ["remoteok"],
        "writing_style": p.get("writing_style") or "",
        "resume_url": p.get("resume_url") or "",
        "onboarding_completed": p.get("onboarding_completed") or False,
    }


def update_profile(user_id: str, data: dict) -> dict:
    payload = {
        "name": data.get("name", ""),
        "last_name": data.get("last_name", ""),
        "phone": data.get("phone", ""),
        "keywords": data.get("keywords", []),
        "location": data.get("location", "remote"),
        "job_type": data.get("job_type", "full-time"),
        "platforms": data.get("platforms", ["remoteok"]),
        "writing_style": data.get("writing_style", ""),
    }
    (
        get_supabase()
        .table("profiles")
        .update(payload)
        .eq("user_id", user_id)
        .execute()
    )
    return get_profile(user_id)


def get_connections(user_id: str) -> dict:
    """Возвращает platform connections из поля profiles.connections."""
    res = (
        get_supabase()
        .table("profiles")
        .select("connections")
        .eq("user_id", user_id)
        .execute()
    )
    if res.data:
        return res.data[0].get("connections") or {}
    return {}


def set_connection(user_id: str, platform: str, connected: bool) -> None:
    from datetime import datetime, timezone
    conns = get_connections(user_id)
    conns[platform] = {
        "connected": connected,
        "connected_at": datetime.now(timezone.utc).isoformat() if connected else None,
    }
    (
        get_supabase()
        .table("profiles")
        .update({"connections": conns})
        .eq("user_id", user_id)
        .execute()
    )
