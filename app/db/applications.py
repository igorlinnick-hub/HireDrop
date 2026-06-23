"""Все операции с таблицей applications в Supabase."""

from datetime import date

from app.db.client import get_supabase


def save_application(
    user_id: str,
    job_id: str,
    cover_letter: str = "",
    status: str = "applied",
) -> str:
    res = (
        get_supabase()
        .table("applications")
        .insert(
            {
                "user_id": user_id,
                "job_id": job_id,
                "cover_letter": cover_letter,
                "status": status,
            }
        )
        .execute()
    )
    return res.data[0]["id"] if res.data else ""


def get_history(user_id: str, limit: int = 50) -> list:
    res = (
        get_supabase()
        .table("applications")
        .select("*, jobs(title, company, platform, link, tailored_resume)")
        .eq("user_id", user_id)
        .order("date_applied", desc=True)
        .limit(limit)
        .execute()
    )
    rows = []
    for row in res.data or []:
        job = row.get("jobs") or {}
        rows.append(
            {
                "id": row["id"],
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "platform": job.get("platform", ""),
                "link": job.get("link", ""),
                "date_applied": row["date_applied"],
                "status": row["status"],
                "cover_letter": row.get("cover_letter", ""),
                "tailored_resume": job.get("tailored_resume") or "",
            }
        )
    return rows


def count_applications(user_id: str) -> int:
    res = (
        get_supabase()
        .table("applications")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .execute()
    )
    return res.count or 0


def count_today(user_id: str) -> int:
    today = date.today().isoformat()
    res = (
        get_supabase()
        .table("applications")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("date_applied", today)
        .execute()
    )
    return res.count or 0


def update_status(application_id: str, status: str) -> bool:
    res = (
        get_supabase()
        .table("applications")
        .update({"status": status})
        .eq("id", application_id)
        .execute()
    )
    return bool(res.data)


def find_by_company_all_users(company_fragment: str) -> list:
    """Find applied/pending applications matching a company name fragment (case-insensitive)."""
    res = (
        get_supabase()
        .table("applications")
        .select("id, user_id, status, jobs(company)")
        .ilike("jobs.company", f"%{company_fragment}%")
        .in_("status", ["applied", "pending"])
        .limit(10)
        .execute()
    )
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "status": row["status"],
            "company": (row.get("jobs") or {}).get("company", ""),
        }
        for row in (res.data or [])
        if row.get("jobs")
    ]


def count_today_by_platform(user_id: str) -> dict:
    today = date.today().isoformat()
    res = (
        get_supabase()
        .table("applications")
        .select("*, jobs(platform)")
        .eq("user_id", user_id)
        .gte("date_applied", today)
        .execute()
    )
    counts: dict = {}
    for row in res.data or []:
        platform = (row.get("jobs") or {}).get("platform", "unknown")
        counts[platform] = counts.get(platform, 0) + 1
    return counts
