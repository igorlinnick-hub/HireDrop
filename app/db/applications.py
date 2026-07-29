"""Все операции с таблицей applications в Supabase."""

from datetime import date

from app.db.client import get_supabase


def save_application(
    user_id: str,
    job_id: str,
    cover_letter: str = "",
    status: str = "applied",
    job_title: str = "",
    company: str = "",
    platform: str = "",
    job_url: str = "",
) -> str:
    # Snapshot the display fields ONTO the history row (P3 counter integrity,
    # migrations/2026-07-29_applications_snapshot.sql): history must survive the
    # jobs row disappearing — job_id is a live join key, not the source of truth.
    row = {
        "user_id": user_id,
        "job_id": job_id,
        "cover_letter": cover_letter,
        "status": status,
        "job_title": job_title,
        "company": company,
        "platform": platform,
        "job_url": job_url,
    }
    try:
        res = get_supabase().table("applications").insert(row).execute()
    except Exception:
        # DEPLOY-BEFORE-MIGRATION SAFETY: if the snapshot columns don't exist yet,
        # PostgREST rejects the whole insert — losing the history row would be worse
        # than losing the snapshot. Fall back to the legacy shape; the migration's
        # backfill fills the snapshots later.
        legacy = {k: row[k] for k in ("user_id", "job_id", "cover_letter", "status")}
        res = get_supabase().table("applications").insert(legacy).execute()
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
        # Snapshot fields first (survive job deletion — P3); joined jobs row is the
        # fallback for legacy rows and the only source of tailored_resume.
        job = row.get("jobs") or {}
        rows.append(
            {
                "id": row["id"],
                "title": row.get("job_title") or job.get("title", ""),
                "company": row.get("company") or job.get("company", ""),
                "platform": row.get("platform") or job.get("platform", ""),
                "link": row.get("job_url") or job.get("link", ""),
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


def update_status(application_id: str, status: str, user_id: str) -> bool:
    """Update an application's status, scoped to its owner.

    user_id is required (not optional) so this can never cross tenants: even
    the cross-user email-matching flow must pass the owning user_id from the
    row returned by find_by_company_all_users().
    """
    res = (
        get_supabase()
        .table("applications")
        .update({"status": status})
        .eq("id", application_id)
        .eq("user_id", user_id)
        .execute()
    )
    return bool(res.data)


def _escape_like(s: str) -> str:
    """Escape LIKE/ILIKE wildcards so user/email-derived text can't widen the match.

    Without this, a company fragment like "A%B" (from a crafted email subject)
    becomes a broad wildcard and matches far more applications than intended.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_by_company_all_users(company_fragment: str) -> list:
    """Find applied/pending applications matching a company name fragment (case-insensitive)."""
    res = (
        get_supabase()
        .table("applications")
        .select("id, user_id, status, jobs(company)")
        .ilike("jobs.company", f"%{_escape_like(company_fragment)}%")
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
