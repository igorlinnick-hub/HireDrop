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
    "linkedin_url": "",
    "portfolio_url": "",
    "work_authorized_us": None,
    "needs_sponsorship": None,
    "notice_period": "",
    "english_level": "",
    # Mailing address — ZipRecruiter's contact step won't advance without it (see
    # migrations/add_profile_address.sql). `location` is a SEARCH preference, not an
    # address, so it can't stand in for these.
    "street_address": "",
    "city": "",
    "state": "",
    "postal_code": "",
    # Current employment — "current company / employer / job title" is the single
    # biggest hand-back cause on ATS forms (12 of 21 blanks on the 320-form measure,
    # see migrations/add_current_employment.sql).
    "current_employer": "",
    "current_title": "",
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
        "search_radius_miles": p.get("search_radius_miles"),
        # URL + screener-answer fields the extension's deterministic handlers read.
        # linkedin_url was WRITTEN by update_profile but never returned here — the
        # extension saw an empty profile.linkedin_url and handed back the single most
        # frequent required question (unfilledLedger top-1, 2026-08-09).
        "linkedin_url": p.get("linkedin_url") or "",
        "portfolio_url": p.get("portfolio_url") or "",
        "work_authorized_us": p.get("work_authorized_us"),
        "needs_sponsorship": p.get("needs_sponsorship"),
        "notice_period": p.get("notice_period") or "",
        "english_level": p.get("english_level") or "",
        "street_address": p.get("street_address") or "",
        "city": p.get("city") or "",
        "state": p.get("state") or "",
        "postal_code": p.get("postal_code") or "",
        "current_employer": p.get("current_employer") or "",
        "current_title": p.get("current_title") or "",
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
    # URL fields (LinkedIn / portfolio) for ATS applications — only write them when the
    # caller actually provided them, so a partial save (e.g. search-prefs) never wipes them.
    for k in (
        "linkedin_url",
        "portfolio_url",
        "work_authorized_us",
        "needs_sponsorship",
        "notice_period",
        "english_level",
        "street_address",
        "city",
        "state",
        "postal_code",
        "current_employer",
        "current_title",
    ):
        if k in data:
            payload[k] = data[k]
    (get_supabase().table("profiles").update(payload).eq("user_id", user_id).execute())
    return get_profile(user_id)


def fill_current_employment_if_blank(user_id: str, employer: str, title: str) -> dict:
    """Seed current_employer / current_title from the resume the user uploaded.

    Only fills what is EMPTY — a value the user typed in Settings always wins, and a
    re-generated resume never overwrites their correction. Returns what was written.

    Why this exists: a profile field nobody fills is worth nothing. The mailing address
    shipped 2026-08-15 and three weeks later exactly 1 of 28 profiles had one, while
    'current employer/title' is the biggest hand-back cause on real forms. The resume
    already names both — experience[0] — so we take them from there instead of asking.
    """
    filled = {}
    current = get_profile(user_id)
    if employer and employer.strip() and not current.get("current_employer"):
        filled["current_employer"] = employer.strip()[:200]
    if title and title.strip() and not current.get("current_title"):
        filled["current_title"] = title.strip()[:200]
    if filled:
        get_supabase().table("profiles").update(filled).eq("user_id", user_id).execute()
    return filled


def fill_address_if_blank(user_id: str, city: str, state: str, postal_code: str) -> dict:
    """Seed city/state/zip from the resume's contact block — same contract as
    fill_current_employment_if_blank: only EMPTY fields, user input always wins.

    Resumes almost never carry a street address, so street stays user-supplied and is
    asked for only when a form actually requires it (honest hand-back) — but the
    "City, ST ZIP" most resumes DO carry covers the usual contact step (ZR included)
    without the user ever visiting Settings.
    """
    candidate = {"city": city, "state": state, "postal_code": postal_code}
    filled = {}
    current = get_profile(user_id)
    for col, val in candidate.items():
        v = (val or "").strip()
        if v and not current.get(col):
            filled[col] = v[:100]
    if filled:
        get_supabase().table("profiles").update(filled).eq("user_id", user_id).execute()
    return filled


def update_apply_mode(user_id: str, mode: str, ideal_job_description: str | None = None) -> None:
    """Switch apply mode. Clears ideal_job_description when switching away from precise."""
    payload: dict = {"apply_mode": mode}
    if mode == "precise" and ideal_job_description:
        payload["ideal_job_description"] = ideal_job_description.strip() or None
    elif mode != "precise":
        payload["ideal_job_description"] = None
    get_supabase().table("profiles").update(payload).eq("user_id", user_id).execute()


def update_salary(
    user_id: str, salary_min: int | None, salary_max: int | None, listed_only: bool
) -> None:
    """Optional salary-range filter (annual USD). None clears a bound — an empty
    filter means "don't filter by salary". Does not touch other profile columns."""
    get_supabase().table("profiles").update(
        {
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_listed_only": bool(listed_only),
        }
    ).eq("user_id", user_id).execute()


def update_radius(user_id: str, miles: int | None) -> None:
    """Optional non-remote search radius (miles). None clears it. Does not touch
    other profile columns."""
    get_supabase().table("profiles").update(
        {
            "search_radius_miles": miles,
        }
    ).eq("user_id", user_id).execute()


def update_ats(user_id: str, data: dict) -> None:
    """Partial update — only ATS fields. Does not touch other profile columns."""
    payload = {
        k: v
        for k, v in data.items()
        if k in ("ats_score", "ats_issues", "ats_resume_url", "ats_approved", "ats_checked_at")
    }
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
