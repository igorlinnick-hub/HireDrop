"""Hand-backs — the applications the filler could not finish, as a live to-do list.

The filler's invariant is "submitted-complete-and-honest OR handed back with a reason".
The hand-back half used to exist only as an activity-log line, which meant it scrolled
away: the one thing in the product that actually needs the user's hands was the thing
hardest to find (Igor, 09-21). A log line also cannot be marked done.

One row per unfinished application. `resolved_at IS NULL` = still waiting. Both surfaces
that show this — the extension popup and the dashboard rail — read THIS, so their counts
cannot disagree.
"""

from datetime import UTC, datetime

from app.db.client import get_supabase

# The backend runs as service_role and bypasses RLS, so every query here filters
# user_id explicitly. This table is a to-do list keyed to a person; a missing filter
# would hand one user another's job links (IDOR — the project's #1 risk class).


def add(user_id: str, job: dict) -> dict | None:
    """Record a hand-back. Idempotent per open URL — the partial unique index means a
    job handed back twice (a re-run that hit the same wall) updates rather than stacks,
    so the list counts JOBS waiting, not attempts."""
    row = {
        "user_id": user_id,
        "job_title": (job.get("job_title") or "")[:300],
        "company": (job.get("company") or "")[:200],
        "url": (job.get("url") or "")[:1000],
        "platform": (job.get("platform") or "")[:40],
        "reason": (job.get("reason") or "")[:500],
        # Screens actually completed before the wall. Bounded because it drives a
        # progress bar: a bogus 900 would render a lie, and the cap is cheaper than
        # trusting a client number.
        "steps_done": max(0, min(int(job.get("steps_done") or 0), 30)),
    }
    res = (
        get_supabase()
        .table("handbacks")
        .upsert(row, on_conflict="user_id,url", ignore_duplicates=False)
        .execute()
    )
    return (res.data or [None])[0]


def list_open(user_id: str, limit: int = 20) -> list[dict]:
    res = (
        get_supabase()
        .table("handbacks")
        .select("id, job_title, company, url, platform, reason, steps_done, created_at")
        .eq("user_id", user_id)
        .is_("resolved_at", "null")
        .order("created_at", desc=True)
        .limit(min(max(limit, 1), 100))
        .execute()
    )
    return res.data or []


def resolve(user_id: str, handback_id: str) -> bool:
    """Mark one as finished. Returns False when nothing matched — a wrong id and someone
    else's id are the same answer here on purpose: never confirm another user's row exists.
    """
    res = (
        get_supabase()
        .table("handbacks")
        .update({"resolved_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .eq("id", handback_id)
        .is_("resolved_at", "null")
        .execute()
    )
    return bool(res.data)
