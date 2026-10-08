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
    # Skills-style (second) resume + the explicit default-resume dial.
    # default_resume None = legacy behavior (ats_approved decides).
    "skills_resume_url": None,
    "skill_groups": None,
    "default_resume": None,
    "skills_description": "",
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
    # Answered once in the pre-Start form (modules/employer_answers.py).
    "country": "",
    "no_linkedin": False,
    # Education — Greenhouse's "School" / "Degree" (migrations/add_employer_answer_fields.sql).
    "school": "",
    "degree": "",
    "no_degree": False,
    # What the user said to tell an employer who asks about pay — NOT the salary_min
    # filter below, which only decides which jobs they see.
    "salary_expectation": "",
    "no_salary_expectation": False,
    # Answers the person gave once and we reuse (modules/personal_facts.py).
    "personal_facts": [],
    # Search gates read by on_search_filter (deck, auto ATS queue, night shift).
    "salary_min": None,
    "salary_max": None,
    "salary_listed_only": False,
    "work_setting": "",
    # The stored structure behind the generated resume — resume_text_for() reads it.
    "ats_structure": None,
    # What the uploaded resume states, keyed by the resume it was read from
    # (modules/ai_resume_facts.py, migrations/add_resume_facts.sql). None before the
    # column exists — `select *` simply has no such key, so nothing here can fail on it.
    "resume_facts": None,
}


def _own_path(user_id: str, path) -> str:
    """A stored resume path — only if it lies inside this user's own storage folder.

    `profiles.resume_url` is written by the BROWSER (the wizard uploads the file and saves
    the path through supabase-js; RLS lets a user update any column of their own row).
    The server then reads that path with the service key, which no storage policy
    restrains: `resolved_resume_path` signs a download URL for it, `load_resume_text`
    feeds it to every model call. So a user who saved someone else's
    `<uuid>/resume.pdf` as their own path was handed that person's resume — as a file,
    and as text inside their cover letters and answers.

    Every upload this product has ever made is `<user_id>/…` (checked on the live table:
    15 of 15), so a path anywhere else is not a resume of theirs and is read as "none".
    """
    path = str(path or "")
    inside = path.startswith(f"{user_id}/") and ".." not in path and "\\" not in path
    return path if inside else ""


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
        "resume_url": _own_path(user_id, p.get("resume_url")),
        "onboarding_completed": p.get("onboarding_completed") or False,
        "ats_score": p.get("ats_score"),
        "ats_issues": p.get("ats_issues") or [],
        "ats_resume_url": p.get("ats_resume_url"),
        "ats_approved": p.get("ats_approved") or False,
        "ats_checked_at": p.get("ats_checked_at"),
        "skills_resume_url": p.get("skills_resume_url"),
        "skill_groups": p.get("skill_groups"),
        "default_resume": p.get("default_resume"),
        "skills_description": p.get("skills_description") or "",
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
        "country": p.get("country") or "",
        "no_linkedin": bool(p.get("no_linkedin")),
        "school": p.get("school") or "",
        "degree": p.get("degree") or "",
        "no_degree": bool(p.get("no_degree")),
        "salary_expectation": p.get("salary_expectation") or "",
        "no_salary_expectation": bool(p.get("no_salary_expectation")),
        # Written by update_salary / update_profile / update_ats but not returned here
        # until 09-30, so every reader saw None: on_search_filter's salary and
        # work-setting gates never fired (deck, auto queue, night shift), and
        # resume_text_for() never read the resume the user corrected in the editor (#222).
        # Tests patched get_profile with dicts that already carried these keys, which is
        # how it stayed hidden — test_get_profile_fields.py runs the real function.
        "salary_min": p.get("salary_min"),
        "salary_max": p.get("salary_max"),
        "salary_listed_only": bool(p.get("salary_listed_only")),
        "work_setting": p.get("work_setting") or "",
        "ats_structure": p.get("ats_structure"),
        "resume_facts": p.get("resume_facts"),
        # What the person told us about their circumstances (modules/personal_facts.py):
        # read by the screener answerer, the cover letter and Drop. A missing column
        # (before migrations/add_personal_facts.sql) reads as none.
        "personal_facts": p.get("personal_facts") or [],
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
        "work_setting",
        "country",
        "school",
        "degree",
        "no_degree",
        "salary_expectation",
        "no_salary_expectation",
    ):
        if k in data:
            payload[k] = data[k]
    (get_supabase().table("profiles").update(payload).eq("user_id", user_id).execute())
    return get_profile(user_id)


def update_employer_answers(user_id: str, answers: dict) -> dict:
    """Partial write of the pre-Start answers — touches only the keys given, already
    cleaned by modules.employer_answers.clean. update_profile can't be reused: it resets
    name/keywords/location to defaults whenever the caller omits them."""
    if answers:
        get_supabase().table("profiles").update(answers).eq("user_id", user_id).execute()
    return get_profile(user_id)


def fill_postal_if_blank(user_id: str, postal_code: str, resume_city: str) -> dict:
    """Seed the zip from the resume's contact block — the ZipRecruiter contact step needs
    one (#110/#152) and a hand-back there loses the application.

    The only resume value still written without asking, because it is not one of the
    employer questions and has no form to confirm it in. So it is held to the person's
    OWN city: written only when the zip is empty and the resume's city is the city they
    answered — a Miami zip under the Austin they typed would be a false address.
    """
    postal = (postal_code or "").strip()
    current = get_profile(user_id)
    city = str(current.get("city") or "").strip().lower()
    if (
        not postal
        or current.get("postal_code")
        or not city
        or city != (resume_city or "").strip().lower()
    ):
        return {}
    get_supabase().table("profiles").update({"postal_code": postal[:100]}).eq(
        "user_id", user_id
    ).execute()
    return {"postal_code": postal[:100]}


def save_resume_facts(user_id: str, facts: dict) -> bool:
    """Keep what the resume states (ai_resume_facts.record) so it is read once per upload.

    Best-effort: False when it could not be stored — before migrations/add_resume_facts.sql
    is applied PostgREST refuses the unknown column (PGRST204) — and the caller simply
    reads the resume again next time.
    """
    try:
        get_supabase().table("profiles").update({"resume_facts": facts}).eq(
            "user_id", user_id
        ).execute()
        return True
    except Exception as e:  # noqa: BLE001 — a cache write never fails the request
        print(f"[profile] resume_facts not stored: {e}")
        return False


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
        if k
        in (
            "ats_score",
            "ats_issues",
            "ats_resume_url",
            "ats_approved",
            "ats_checked_at",
            "ats_structure",
        )
    }
    if payload:
        get_supabase().table("profiles").update(payload).eq("user_id", user_id).execute()


def update_skills_resume(user_id: str, data: dict) -> None:
    """Partial update — only skills-resume fields. Does not touch other columns."""
    payload = {
        k: v
        for k, v in data.items()
        if k in ("skills_resume_url", "skill_groups", "default_resume", "skills_description")
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
