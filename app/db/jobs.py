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
