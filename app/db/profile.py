"""Операции с таблицей profiles в Supabase."""

from datetime import UTC

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
    "ats_score": None,
    "ats_issues": [],
    "ats_resume_url": None,
    "ats_approved": False,
    "ats_checked_at": None,
    "apply_mode": "standard",
    "ideal_job_description": None,
}


def get_profile(user_id: str) -> dict:
    res = get_supabase().table("profiles").select("*").eq("user_id", user_id).execute()
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
        "ats_score": p.get("ats_score"),
        "ats_issues": p.get("ats_issues") or [],
        "ats_resume_url": p.get("ats_resume_url"),
        "ats_approved": p.get("ats_approved") or False,
        "ats_checked_at": p.get("ats_checked_at"),
        "apply_mode": p.get("apply_mode") or "standard",
        "ideal_job_description": p.get("ideal_job_description") or None,
    }


def update_profile(user_id: str, data: dict) -> dict:
    # apply_mode and ideal_job_description are managed via update_apply_mode() —
    # intentionally excluded here so profile saves never silently reset the apply mode.
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
    (get_supabase().table("profiles").update(payload).eq("user_id", user_id).execute())
    return get_profile(user_id)


def update_apply_mode(user_id: str, mode: str, ideal_job_description: str | None = None) -> None:
    """Switch apply mode. Clears ideal_job_description when switching away from precise."""
    payload: dict = {"apply_mode": mode}
    if mode == "precise" and ideal_job_description:
        payload["ideal_job_description"] = ideal_job_description.strip() or None
    elif mode != "precise":
        payload["ideal_job_description"] = None
    get_supabase().table("profiles").update(payload).eq("user_id", user_id).execute()


def update_ats(user_id: str, data: dict) -> None:
    """Partial update — only ATS fields. Does not touch other profile columns."""
    payload = {k: v for k, v in data.items() if k in (
        "ats_score", "ats_issues", "ats_resume_url", "ats_approved", "ats_checked_at"
    )}
    if payload:
        get_supabase().table("profiles").update(payload).eq("user_id", user_id).execute()


def get_connections(user_id: str) -> dict:
    """Возвращает platform connections из поля profiles.connections."""
    res = get_supabase().table("profiles").select("connections").eq("user_id", user_id).execute()
    if res.data:
        return res.data[0].get("connections") or {}
    return {}


def set_connection(user_id: str, platform: str, connected: bool) -> None:
    from datetime import datetime

    conns = get_connections(user_id)
    conns[platform] = {
        "connected": connected,
        "connected_at": datetime.now(UTC).isoformat() if connected else None,
    }
    (
        get_supabase()
        .table("profiles")
        .update({"connections": conns})
        .eq("user_id", user_id)
        .execute()
    )
