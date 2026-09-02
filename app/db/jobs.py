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


_SCORE_COLUMNS = ("score", "ai_verdict", "ai_flags", "ats_keywords", "ats_match_pct")


def _bulk_row(user_id: str, job: dict) -> dict:
    """Uniform column set for a bulk upsert — PostgREST rejects a batch whose rows
    carry different keys, so score fields are always present (null when unscored)."""
    import uuid

    return {
        "user_id": user_id,
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "link": job.get("link") or f"manual:{uuid.uuid4()}",
        "status": job.get("status", "new"),
        "platform": job.get("platform", "unknown"),
        "description": job.get("description", ""),
        "location": job.get("location", ""),
        "job_type": job.get("job_type", ""),
        "score": job.get("score"),
        "ai_verdict": job.get("ai_verdict", ""),
        "ai_flags": job.get("ai_flags", []),
        "ats_keywords": job.get("ats_keywords", []),
        "ats_match_pct": job.get("ats_match_pct", 0),
    }


def save_jobs_bulk(user_id: str, jobs: list) -> int:
    """Upsert a whole discovery/harvest batch in ONE round-trip, score fields inline —
    instead of 2 sequential HTTP calls per row (save_job + update_job_score), which at
    a 160-job ATS pass meant ~320 calls. Dedupes by link inside the batch (Postgres
    can't touch the same row twice in one upsert). Degrades on failure: score-less
    rows (pre-migration DBs), then the old per-row path, so a bad batch slows down
    instead of losing the harvest."""
    seen: set = set()
    rows = []
    for job in jobs:
        row = _bulk_row(user_id, job)
        if row["link"] in seen:
            continue
        seen.add(row["link"])
        rows.append(row)
    if not rows:
        return 0

    for attempt in ("scored", "core"):
        try:
            get_supabase().table("jobs").upsert(rows, on_conflict="user_id,link").execute()
            return len(rows)
        except Exception as e:
            print(f"[jobs] bulk upsert ({attempt}) failed, downgrading: {e}")
            rows = [{k: v for k, v in r.items() if k not in _SCORE_COLUMNS} for r in rows]

    saved = 0
    for job in jobs:
        job_id = save_job(
            user_id=user_id,
            title=job.get("title", ""),
            company=job.get("company", ""),
            link=job.get("link", ""),
            status=job.get("status", "new"),
            platform=job.get("platform", "unknown"),
            description=job.get("description", ""),
            location=job.get("location", ""),
            job_type=job.get("job_type", ""),
        )
        if job_id:
            saved += 1
            if job.get("score") is not None:
                update_job_score(
                    job_id,
                    user_id,
                    job["score"],
                    job.get("ai_verdict", ""),
                    job.get("ai_flags", []),
                    job.get("ats_keywords", []),
                    job.get("ats_match_pct", 0),
                )
    return saved


def existing_links(user_id: str, links: list) -> set:
    """Which of these links are already saved — chunked IN-queries (40 links each)
    instead of one job_exists round-trip per link. Chunked because PostgREST puts
    the IN-list into the URL and job links run long."""
    links = [link for link in links if link]
    out: set = set()
    for i in range(0, len(links), 40):
        res = (
            get_supabase()
            .table("jobs")
            .select("link")
            .eq("user_id", user_id)
            .in_("link", links[i : i + 40])
            .execute()
        )
        out.update(r["link"] for r in (res.data or []))
    return out


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
    user_id: str,
    score: int,
    verdict: str,
    flags: list,
    ats_keywords: list = None,
    ats_match_pct: int = 0,
) -> None:
    """Store AI scoring result. Silently skips if columns don't exist yet.
    user_id-scoped (service_role bypasses RLS) so a job_id can only update the caller's
    own row — defense-in-depth even though callers already pass user-scoped ids."""
    try:
        get_supabase().table("jobs").update({
            "score": score,
            "ai_verdict": verdict,
            "ai_flags": flags,
            "ats_keywords": ats_keywords or [],
            "ats_match_pct": ats_match_pct,
        }).eq("id", job_id).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[jobs] update_job_score skipped (run migration?): {e}")


def update_job_description(job_id: str, user_id: str, description: str) -> None:
    """Backfill a job's description (e.g. GH jobs saved before ?content=true).
    user_id-scoped so a job_id only updates the caller's own row."""
    try:
        get_supabase().table("jobs").update({
            "description": (description or "")[:5000],
        }).eq("id", job_id).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[jobs] update_job_description skipped: {e}")


def update_tailored_resume(job_id: str, user_id: str, tailored_resume: str) -> None:
    try:
        get_supabase().table("jobs").update({
            "tailored_resume": tailored_resume,
        }).eq("id", job_id).eq("user_id", user_id).execute()
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
