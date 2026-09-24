"""Все операции с таблицей jobs в Supabase."""

import contextlib
from datetime import date

from app.db.client import fetch_paged, get_supabase


def _pool_query(columns: str, user_id: str):
    """The pool read both callers share, ordered so paging can't reshuffle rows."""

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("jobs")
            .select(columns)
            .eq("user_id", user_id)
            .order("date_found", desc=True)
            .order("id")
            .range(start, end)
        )

    return build


def get_jobs(user_id: str, limit: int = 20000) -> list:
    """The user's whole pool, newest first — paged past PostgREST's silent row cap.

    A bare select stopped at 1000 rows, so once the pool outgrew it every reader of this
    function — the dashboard table, the auto ATS queue, the tap deck — was quietly working
    on the newest thousand only: applied rows fell off the dashboard count and older
    approved/new inventory became unreachable (read-side twin of #196's cap).
    """
    return fetch_paged(_pool_query("*", user_id), limit)


# Exactly what the dashboard's Job Listings table renders (jobflow-website
# lib/types.ts::Job minus `description`). The pool carries up to 5000 chars of posting
# text per row, which is half the weight of a `select *` and nothing the table draws —
# on a 1300-row pool that is 1.3 MB shipped to the browser to be thrown away. The AI
# lanes that DO need the text read the full rows via get_jobs().
_LISTING_COLUMNS = (
    "id, title, company, platform, status, date_found, link, score, ai_verdict, "
    "ai_flags, ats_keywords, ats_match_pct, tailored_resume"
)


def get_jobs_for_listing(user_id: str, limit: int = 20000) -> list:
    """get_jobs, minus the columns the dashboard table never renders."""
    return fetch_paged(_pool_query(_LISTING_COLUMNS, user_id), limit)


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

    payload = {
        "user_id": user_id,
        "title": title,
        "company": company,
        "link": link,
        "status": status,
        "platform": platform,
    }
    # Only send the scraped fields when we actually have them. PostgREST builds the
    # ON CONFLICT DO UPDATE clause from the keys present in the payload, so sending an
    # empty description here overwrites the text discovery scraped earlier.
    #
    # That is exactly what happened on every apply: /applications/save calls save_job
    # without a description, so submitting wiped the posting of the job just applied to —
    # and cover letters, ai_job_scorer and ai_fit_judge all read that column, so each
    # submit quietly degraded the quality of the applications that came after it.
    for key, value in (
        ("description", description),
        ("location", location),
        ("job_type", job_type),
    ):
        if value:
            payload[key] = value

    res = get_supabase().table("jobs").upsert(payload, on_conflict="user_id,link").execute()
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


def all_links(user_id: str, limit: int = 20000) -> set:
    """EVERY link already in this user's pool, in one paginated sweep.

    `existing_links` answers "are THESE saved?" — one IN-query per 40 links. Discovery
    now needs the mirror question ("what have we NOT seen?") over the whole collected
    sweep, which is ~500-5000 links: that would be 12-125 round-trips. The pool is a
    couple of thousand rows, so pulling the link column whole is two queries.
    """

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("jobs")
            .select("link")
            .eq("user_id", user_id)
            .order("id")
            .range(start, end)
        )

    return {r["link"] for r in fetch_paged(build, limit) if r.get("link")}


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


def update_job_status(user_id: str, job_id: str, status: str) -> int:
    """Set the row's status; returns how many rows changed (0 = nothing matched).

    The count is the point: a swipe that writes nothing looks exactly like a swipe that
    worked, and the card is already gone from the deck. Callers must be able to tell.
    """
    res = (
        get_supabase()
        .table("jobs")
        .update({"status": status})
        .eq("id", job_id)
        .eq("user_id", user_id)
        .execute()
    )
    return len(res.data or [])


def mark_applied_by_link(user_id: str, url: str, status: str = "applied") -> int:
    """Close out every OTHER pool row naming the same posting. Returns rows healed.

    The upsert in save_job keys on the exact link, so applying via a different spelling of
    the same URL (job-boards vs boards, ?gh_jid, /application) writes a second row and
    leaves the swiped one `approved` forever — re-queued every run, and double-applied on
    a browser profile whose local dedup set is empty. Identity comes from the board's
    posting id (modules.job_identity) when the URL carries one; slug URLs without an id
    fall back to exact normalized-link equality — never anything fuzzier.
    """
    from modules.job_identity import job_identity, normalized_link

    token = job_identity(url)
    if token:
        # The id itself narrows the read; job_identity then confirms each candidate, so
        # a substring collision (an id inside another URL) can't match.
        needle = token.split(":", 1)[1]

        def confirms(link: str) -> bool:
            return job_identity(link) == token

    else:
        # Slug URL, no posting id: heal only rows whose normalized link (lowercase host,
        # apply tails / trailing slash / tracking query stripped) is EXACTLY the same.
        # A host-only URL or a slug too short to narrow the read is not safe to heal.
        norm = normalized_link(url)
        if not norm or "/" not in norm:
            return 0
        needle = norm.rsplit("?", 1)[0].rsplit("/", 1)[-1]
        if len(needle) < 6:
            return 0

        def confirms(link: str) -> bool:
            return normalized_link(link) == norm

    try:
        res = (
            get_supabase()
            .table("jobs")
            .select("id, link, status")
            .eq("user_id", user_id)
            .ilike("link", f"%{needle}%")
            .execute()
        )
    except Exception:  # noqa: BLE001 — healing is best-effort; never break an apply
        return 0

    ids = [
        r["id"]
        for r in (res.data or [])
        if r.get("status") in ("new", "approved") and confirms(r.get("link"))
    ]
    if not ids:
        return 0
    with contextlib.suppress(Exception):
        get_supabase().table("jobs").update({"status": status}).in_("id", ids).eq(
            "user_id", user_id
        ).execute()
        return len(ids)
    return 0


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
        get_supabase().table("jobs").update(
            {
                "score": score,
                "ai_verdict": verdict,
                "ai_flags": flags,
                "ats_keywords": ats_keywords or [],
                "ats_match_pct": ats_match_pct,
            }
        ).eq("id", job_id).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[jobs] update_job_score skipped (run migration?): {e}")


def update_job_description(job_id: str, user_id: str, description: str) -> None:
    """Backfill a job's description (e.g. GH jobs saved before ?content=true).
    user_id-scoped so a job_id only updates the caller's own row."""
    try:
        get_supabase().table("jobs").update(
            {
                "description": (description or "")[:5000],
            }
        ).eq("id", job_id).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[jobs] update_job_description skipped: {e}")


def update_tailored_resume(job_id: str, user_id: str, tailored_resume: str) -> None:
    try:
        get_supabase().table("jobs").update(
            {
                "tailored_resume": tailored_resume,
            }
        ).eq("id", job_id).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[jobs] update_tailored_resume skipped: {e}")


def update_tailored_resume_pdf(job_id: str, pdf_path: str, user_id: str) -> None:
    try:
        get_supabase().table("jobs").update(
            {
                "tailored_resume_pdf_url": pdf_path,
            }
        ).eq("id", job_id).eq("user_id", user_id).execute()
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


def save_description(user_id: str, link: str, description: str, **new_row) -> str:
    """Store the posting text for a job the user is looking at RIGHT NOW.

    Indeed's pool rows carry the search-card snippet, not the posting: measured
    2026-09-20, 0 of 387 rows held real text and the longest was ~90 chars. The full
    text is on the detail page the extension already parses — it just never reached
    the row, so tailoring, the fit judge and the interview kit all read a salary
    string for 86% of our applications.

    Deliberately NOT save_job(): that upsert always sends `status`, so writing a
    description through it would reset an `applied`/`skipped`/`approved` row to `new`
    and resurrect it into the walk. This updates the text and nothing else, and only
    falls back to an insert when the posting isn't in the pool at all (a by-link
    apply). Matching mirrors get_by_link — exact URL, then the Indeed jk key, which is
    what actually identifies a posting across query-string spellings.

    Returns the row id, or "" if nothing was written.
    """
    row = get_by_link(user_id, link)
    if row:
        res = (
            get_supabase()
            .table("jobs")
            .update({"description": description})
            .eq("id", row["id"])
            .eq("user_id", user_id)  # service_role bypasses RLS — this filter is the check
            .execute()
        )
        return row["id"] if res.data else ""

    payload = {
        "user_id": user_id,
        "link": link,
        "description": description,
        "status": "new",
        "title": new_row.get("title", ""),
        "company": new_row.get("company", ""),
        "platform": new_row.get("platform", "unknown"),
    }
    res = get_supabase().table("jobs").insert(payload).execute()
    return res.data[0]["id"] if res.data else ""


def mark_dead_link(user_id: str, link: str) -> int:
    """Flip every row for this posting out of the pool — the board says it's gone.

    A dead posting (expired, pulled, or a stale seed row) otherwise stays `new` forever and
    the walk re-opens it on every run: before 2026-09-06 that cost 31 minutes of frozen
    campaign per hit, and even with the extension's dead-link guard it still burns a page
    load each time. Matching mirrors get_by_link — exact URL, then the Indeed jk key, which
    is what actually identifies the posting (the same job arrives with different query
    strings from jobspy and from the browser). Returns how many rows moved.
    """
    import re

    ids: list[str] = []
    res = (
        get_supabase().table("jobs").select("id").eq("user_id", user_id).eq("link", link).execute()
    )
    ids += [r["id"] for r in (res.data or [])]
    m = re.search(r"[?&](?:vjk|jk)=([a-z0-9]+)", link, re.IGNORECASE)
    if m:
        res = (
            get_supabase()
            .table("jobs")
            .select("id")
            .eq("user_id", user_id)
            .ilike("link", f"%jk={m.group(1)}%")
            .execute()
        )
        ids += [r["id"] for r in (res.data or [])]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return 0
    # Never resurrect an applied row into `skipped` — the count and the dedup key hang off
    # it. Only postings still waiting in the pool are cleared.
    (
        get_supabase()
        .table("jobs")
        .update({"status": "skipped"})
        .eq("user_id", user_id)
        .in_("id", ids)
        .in_("status", ["new", "approved", "queued"])
        .execute()
    )
    return len(ids)


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


def count_approved_jobs(user_id: str) -> int:
    """Swipes the user approved that are still waiting — nobody has applied to them yet.

    Applying flips the row to `applied`, so an `approved` row is by definition undone work.
    Counted server-side (head query, no rows) because /campaign/status is polled: the point
    is to make a stranded stack VISIBLE, not to pay for it every few seconds.
    """
    res = (
        get_supabase()
        .table("jobs")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", "approved")
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
