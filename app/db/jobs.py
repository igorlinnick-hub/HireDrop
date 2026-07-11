"""Все операции с таблицей jobs в Supabase."""

from datetime import date

from app.db.client import get_supabase


def get_jobs(user_id: str) -> list:
    res = (
        get_supabase()
        .table("jobs")
        .select("*")
        .eq("user_id", user_id)
        .order("date_found", desc=True)
        .execute()
    )
    return res.data or []


def get_job_by_id(user_id: str, job_id: str) -> dict | None:
    res = get_supabase().table("jobs").select("*").eq("id", job_id).eq("user_id", user_id).execute()
    return res.data[0] if res.data else None


def job_exists(user_id: str, link: str) -> bool:
    if not link:
        return False
    res = (
        get_supabase().table("jobs").select("id").eq("user_id", user_id).eq("link", link).execute()
    )
    return len(res.data) > 0


def save_job(
    user_id: str,
    title: str,
    company: str,
    link: str,
    status: str = "new",
    platform: str = "remoteok",
    description: str = "",
    location: str = "",
    job_type: str = "",
) -> str:
    """Upsert job (user_id + link уникальны). Возвращает id записи."""
    # Если link пустой — генерируем уникальный ключ чтобы не было коллизий
    if not link:
        import uuid

        link = f"manual:{uuid.uuid4()}"

    res = (
        get_supabase()
        .table("jobs")
        .upsert(
            {
                "user_id": user_id,
                "title": title,
                "company": company,
                "link": link,
                "status": status,
                "platform": platform,
                "description": description,
                "location": location,
                "job_type": job_type,
            },
            on_conflict="user_id,link",
        )
        .execute()
    )
    return res.data[0]["id"] if res.data else ""


def update_job_status(user_id: str, job_id: str, status: str) -> None:
    (
        get_supabase()
        .table("jobs")
        .update({"status": status})
        .eq("id", job_id)
        .eq("user_id", user_id)
        .execute()
    )


def update_job_score(
    job_id: str,
    score: int,
    verdict: str,
    flags: list,
    ats_keywords: list = None,
    ats_match_pct: int = 0,
) -> None:
    """Store AI scoring result. Silently skips if columns don't exist yet."""
    try:
        get_supabase().table("jobs").update({
            "score": score,
            "ai_verdict": verdict,
            "ai_flags": flags,
            "ats_keywords": ats_keywords or [],
            "ats_match_pct": ats_match_pct,
        }).eq("id", job_id).execute()
    except Exception as e:
        print(f"[jobs] update_job_score skipped (run migration?): {e}")


def update_tailored_resume(job_id: str, tailored_resume: str) -> None:
    try:
        get_supabase().table("jobs").update({
            "tailored_resume": tailored_resume,
        }).eq("id", job_id).execute()
    except Exception as e:
        print(f"[jobs] update_tailored_resume skipped: {e}")


def update_tailored_resume_pdf(job_id: str, pdf_path: str, user_id: str) -> None:
    try:
        get_supabase().table("jobs").update({
            "tailored_resume_pdf_url": pdf_path,
        }).eq("id", job_id).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[jobs] update_tailored_resume_pdf skipped: {e}")


def get_by_link(user_id: str, link: str) -> dict | None:
    """Find a job by URL. Tries exact match first, then matches on Indeed jk key."""
    import re
    res = (
        get_supabase()
        .table("jobs")
        .select("id, tailored_resume_pdf_url")
        .eq("user_id", user_id)
        .eq("link", link)
        .limit(1)
        .execute()
    )
    if res.data:
        return res.data[0]
    # Fallback: match on Indeed jk/vjk parameter (URL formats differ between jobspy and browser)
    m = re.search(r"[?&](?:vjk|jk)=([a-z0-9]+)", link, re.IGNORECASE)
    if m:
        jk = m.group(1)
        res = (
            get_supabase()
            .table("jobs")
            .select("id, tailored_resume_pdf_url")
            .eq("user_id", user_id)
            .ilike("link", f"%jk={jk}%")
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]
    return None


def count_jobs(user_id: str) -> int:
    res = get_supabase().table("jobs").select("id", count="exact").eq("user_id", user_id).execute()
    return res.count or 0


def count_jobs_found_today(user_id: str) -> int:
    today = date.today().isoformat()
    res = (
        get_supabase()
        .table("jobs")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("date_found", today)
        .execute()
    )
    return res.count or 0


def count_new_jobs(user_id: str, platforms: list | None = None) -> int:
    """Кол-во jobs со статусом new (для campaign status)."""
    query = (
        get_supabase()
        .table("jobs")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", "new")
    )
    if platforms:
        query = query.in_("platform", platforms)
    res = query.execute()
    return res.count or 0
