"""Все операции с таблицей applications в Supabase."""

from datetime import UTC, date, datetime, timedelta

from app.db.client import fetch_paged, get_supabase
from modules.job_location import place_label, posting_work_setting


def save_application(
    user_id: str,
    job_id: str,
    cover_letter: str = "",
    status: str = "applied",
    job_title: str = "",
    company: str = "",
    platform: str = "",
    job_url: str = "",
    form_answers: list[dict] | None = None,
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
    if form_answers:
        row["form_answers"] = form_answers
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


def get_history(user_id: str, limit: int = 5000) -> list:
    """All-time application history, newest first — paginated, no silent cut.

    This used to stop at 50 rows while the dashboard's "Total Applied" card counts the
    whole table (count_applications), so the moment a user crossed 50 the two surfaces
    would diverge and the History page's own "Total applied" metric (computed from this
    list's length) would freeze. Same silent-cap class as get_jobs' 1000-row fix.
    Secondary order on id keeps pages stable when date_applied ties.
    """

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("applications")
            .select(
                "*, jobs(title, company, platform, link, location, tailored_resume,"
                " tailored_resume_pdf_url)"
            )
            .eq("user_id", user_id)
            .order("date_applied", desc=True)
            .order("id")
            .range(start, end)
        )

    data = fetch_paged(build, limit)
    # Lazy import avoids a module-load cycle (resume storage pulls in the client too).
    from app.db import resume as resume_storage

    rows = []
    for row in data:
        # Snapshot fields first (survive job deletion — P3); joined jobs row is the
        # fallback for legacy rows and the only source of tailored_resume.
        job = row.get("jobs") or {}
        # The actual ATS-formatted PDF we submitted — the real artifact a user wants
        # to download. Sign it here (TTL 1h, ample for a page-load→click); user_id
        # scoping in signed_url_from_path refuses cross-tenant paths (defense-in-depth).
        pdf_path = job.get("tailored_resume_pdf_url")
        title = row.get("job_title") or job.get("title", "")
        # Where the job is, for History's place filter. Only the jobs row knows it (the
        # extension heals it through /jobs/describe since ext 1.8.43); empty = we never
        # saw a location, and the page says so rather than guessing one.
        location = (job.get("location") or "").strip()
        rows.append(
            {
                "id": row["id"],
                "title": title,
                "company": row.get("company") or job.get("company", ""),
                "platform": row.get("platform") or job.get("platform", ""),
                "link": row.get("job_url") or job.get("link", ""),
                "date_applied": row["date_applied"],
                "status": row["status"],
                "cover_letter": row.get("cover_letter", ""),
                # What the employer's form asked and what we answered, [{q, a}].
                "form_answers": row.get("form_answers") or [],
                "tailored_resume": job.get("tailored_resume") or "",
                "resume_pdf_url": resume_storage.signed_url_from_path(pdf_path, user_id)
                if pdf_path
                else "",
                "location": location,
                "work_setting": posting_work_setting(location, title),
                "place": place_label(location),
            }
        )
    return rows


def get_for_interview_kit(user_id: str, application_id: str) -> dict | None:
    """One application plus the job text needed to prepare for its interview.

    Returns the snapshot fields merged with the joined jobs row (the join is the only
    source of `description`, which the kit cannot be written without). Scoped by user_id —
    service_role bypasses RLS, so the filter is the IDOR defense.
    """
    res = (
        get_supabase()
        .table("applications")
        .select("*, jobs(title, company, platform, link, description, location)")
        .eq("user_id", user_id)
        .eq("id", application_id)
        .limit(1)
        .execute()
    )
    if not res.data:
        return None
    row = res.data[0]
    job = row.get("jobs") or {}
    return {
        "id": row["id"],
        "status": row["status"],
        "title": row.get("job_title") or job.get("title", ""),
        "company": row.get("company") or job.get("company", ""),
        "platform": row.get("platform") or job.get("platform", ""),
        "link": row.get("job_url") or job.get("link", ""),
        "description": job.get("description") or "",
        "location": job.get("location") or "",
    }


_BUDDY_SELECT = (
    "id, job_title, company, platform, job_url, date_applied, status, cover_letter,"
    " jobs(title, company, platform, location, tailored_resume_pdf_url)"
)


def _buddy_row(row: dict) -> dict:
    job = row.get("jobs") or {}
    return {
        "id": row["id"],
        "title": row.get("job_title") or job.get("title") or "",
        "company": row.get("company") or job.get("company") or "",
        "platform": row.get("platform") or job.get("platform") or "",
        "location": job.get("location") or "",
        "date_applied": row.get("date_applied"),
        "status": row.get("status"),
        "link": row.get("job_url") or "",
        "cover_letter": row.get("cover_letter") or "",
        "has_resume_pdf": bool(job.get("tailored_resume_pdf_url")),
    }


def find_for_buddy(user_id: str, query: str = "", limit: int = 10, scan: int = 300) -> list[dict]:
    """The person's applications for Drop, newest first, optionally filtered by words in
    the title / company / place ("Acme", "designer san diego"). One bounded read, no signed
    URLs (History signs a PDF link per row — Drop only needs to know one exists).
    Scoped by user_id: service_role bypasses RLS."""
    res = (
        get_supabase()
        .table("applications")
        .select(_BUDDY_SELECT)
        .eq("user_id", user_id)
        .order("date_applied", desc=True)
        .order("id")
        .limit(scan)
        .execute()
    )
    words = [w for w in (query or "").lower().split() if w]
    out = []
    for row in res.data or []:
        r = _buddy_row(row)
        hay = f"{r['title']} {r['company']} {r['location']} {r['platform']}".lower()
        if all(w in hay for w in words):
            out.append(r)
        if len(out) >= limit:
            break
    return out


def get_for_buddy(user_id: str, application_id: str) -> dict | None:
    """One of the person's applications with the letter we sent, or None (also for
    someone else's id — same answer on purpose)."""
    res = (
        get_supabase()
        .table("applications")
        .select(_BUDDY_SELECT)
        .eq("user_id", user_id)
        .eq("id", application_id)
        .limit(1)
        .execute()
    )
    return _buddy_row(res.data[0]) if res.data else None


def active_user_ids(since_days: int, cap: int = 20_000) -> list[str]:
    """Accounts that applied to at least one job in the last `since_days`.

    The gate for anything we do FOR a user while they are away (app/pool_sweep.py).
    "Active" is deliberately measured in applications, not logins: a session that
    produced nothing is not a user we should be spending scoring money on.
    """
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("applications")
            .select("user_id, date_applied")
            .gte("date_applied", since)
            # Secondary order by id — PostgREST pages by offset, and ties reshuffle
            # between pages without it (#206): rows get read twice or skipped.
            .order("date_applied", desc=True)
            .order("id")
            .range(start, end)
        )

    rows = fetch_paged(build, cap)
    return list(dict.fromkeys(r["user_id"] for r in rows if r.get("user_id")))


def companies_applied_since(user_id: str, since_days: int, cap: int = 5000) -> list[str]:
    """Company name of every application in the last `since_days`, one entry per
    application (repeats are the point — modules/fit_queue.py counts them against the
    per-company cap). Falls back to the joined job row for history written before the
    company snapshot existed. Raises on a read failure: the caller decides whether a
    queue without this guard is acceptable."""
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("applications")
            .select("company, jobs(company)")
            .eq("user_id", user_id)
            .gte("date_applied", since)
            .order("date_applied", desc=True)
            .order("id")
            .range(start, end)
        )

    out = []
    for r in fetch_paged(build, cap):
        name = r.get("company") or ((r.get("jobs") or {}).get("company") or "")
        if name:
            out.append(name)
    return out


def count_applications(user_id: str) -> int:
    res = (
        get_supabase()
        .table("applications")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .execute()
    )
    return res.count or 0


def count_last_hours(user_id: str, hours: int = 24) -> int:
    """Applications in a rolling window ending now — the dashboard's "Last 24 hours"
    tile. Deliberately NOT the cap counter (count_today / used_today): the cap counts
    by the user's local day, this one slides, so at 9am it still shows last night."""
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    res = (
        get_supabase()
        .table("applications")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("date_applied", since)
        .execute()
    )
    return res.count or 0


def _valid_since(since_iso: str | None) -> str | None:
    """Accept a client-provided ISO instant/date only if it parses — never let a
    malformed query param reach the DB filter (would 500 the whole status call)."""
    if not since_iso:
        return None
    try:
        datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        return since_iso
    except Exception:
        return None


def applied_job_urls(user_id: str, limit: int = 2000, strict: bool = False) -> list[str]:
    """Every URL this user has actually applied to. The server's own answer to "already
    done", independent of the extension's browser-local appliedUrls set — which is empty
    on a fresh Chrome profile and was the only thing standing between a stuck pool row and
    a second application to the same employer.
    """
    # .limit(2000) here used to be a promise the server never kept: PostgREST truncates
    # every response at 1000 rows silently, so past a thousand applications this guard
    # would have started forgetting the oldest ones — and forgetting is what makes us
    # apply to the same employer twice.

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("applications")
            .select("job_url")
            .eq("user_id", user_id)
            .order("date_applied", desc=True)
            .order("id")
            .range(start, end)
        )

    try:
        rows = fetch_paged(build, limit)
    except Exception:  # noqa: BLE001 — the queue must still build without this guard
        # …but a caller about to SUBMIT must not mistake "could not read" for "nothing
        # sent": `strict` lets it refuse instead (the night shift's fail-closed branch
        # was written against this function and could never fire).
        if strict:
            raise
        return []
    return [r["job_url"] for r in rows if r.get("job_url")]


def count_today(user_id: str, since_iso: str | None = None) -> int:
    # "Today" boundary: the client may pass its LOCAL midnight as a UTC ISO instant
    # (since_iso) so a user's day rolls over at THEIR midnight, not the server's UTC
    # (Hawaii user saw yesterday-evening submits counted as "today" — 2026-08-12).
    boundary = _valid_since(since_iso) or date.today().isoformat()
    res = (
        get_supabase()
        .table("applications")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("date_applied", boundary)
        .execute()
    )
    return res.count or 0


def update_status(application_id: str, status: str, user_id: str) -> bool:
    """Update an application's status, scoped to its owner.

    user_id is required (not optional) so this can never cross tenants —
    service_role bypasses RLS, so this filter IS the authorization check.
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


def count_today_by_platform(user_id: str, since_iso: str | None = None) -> dict:
    today = _valid_since(since_iso) or date.today().isoformat()
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


def last_applied_at(user_id: str) -> str | None:
    """Timestamp of the user's most recent application, or None if they never applied.

    The stall watch measures silence from here: as long as this moves, the run is
    producing, whatever the log says.
    """
    res = (
        get_supabase()
        .table("applications")
        .select("date_applied")
        .eq("user_id", user_id)
        .order("date_applied", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0].get("date_applied") if rows else None
