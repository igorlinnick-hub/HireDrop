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


# A question the form asked and we left blank. Bounded like steps_done: these come
# from a scraped page and drive UI, so a 400-option dropdown or a novel-length label
# would be rendered as-is.
_MAX_QUESTIONS = 12
_MAX_LABEL = 300
_MAX_OPTIONS = 20
_MAX_ANSWER = 2000


def _clean_questions(raw) -> list[dict]:
    """Normalise to [{label, options}] and drop anything unusable.

    Accepts both shapes the extension has sent over time: a bare list of label strings
    (collectUnfilledRequired's original output) and the richer dicts. A label is the
    minimum — a question with no text cannot be asked of a human.
    """
    out: list[dict] = []
    for item in (raw or [])[:_MAX_QUESTIONS]:
        if isinstance(item, str):
            label, options = item, []
        elif isinstance(item, dict):
            label = item.get("label") or item.get("question") or ""
            options = item.get("options") or []
        else:
            continue
        label = str(label).strip()[:_MAX_LABEL]
        if not label:
            continue
        opts = [str(o).strip()[:_MAX_LABEL] for o in options if str(o).strip()][:_MAX_OPTIONS]
        out.append({"label": label, "options": opts})
    return out


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
        # What the filler could not answer, as it saw it: [{label, options}]. The
        # extension has always collected this (collectUnfilledRequired) and sent it to
        # the activity log; carrying it HERE is what lets the dashboard ask the human the
        # question instead of just telling them a job needs hands.
        "questions": _clean_questions(job.get("questions")),
        # The pool row, so answering can put it back in the approved queue. Absent for a
        # native walk that was never pool-driven — those rows still belong in the list.
        "job_id": job.get("job_id") or None,
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
        .select(
            "id, job_title, company, url, platform, reason, steps_done, created_at, questions, answers, job_id, requeued_at"
        )
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


def get_open(user_id: str, handback_id: str) -> dict | None:
    """One open row, or None. user_id is in the filter: service_role bypasses RLS."""
    res = (
        get_supabase()
        .table("handbacks")
        .select("id, job_id, questions, answers, job_title, company, url")
        .eq("user_id", user_id)
        .eq("id", handback_id)
        .is_("resolved_at", "null")
        .limit(1)
        .execute()
    )
    return (res.data or [None])[0]


def save_answers(user_id: str, handback_id: str, answers: dict[str, str]) -> dict | None:
    """Store the human's answers and stamp the row as re-queued.

    The row stays OPEN until the retry actually submits it — a re-queued application is
    still unfinished, and draining it here would hide it from the very list that is
    tracking it. `requeued_at` is what the UI reads to say "back in the queue".
    """
    clean = {
        str(q).strip()[:_MAX_LABEL]: str(a).strip()[:_MAX_ANSWER]
        for q, a in (answers or {}).items()
        if str(q).strip() and str(a).strip()
    }
    if not clean:
        return None
    res = (
        get_supabase()
        .table("handbacks")
        .update({"answers": clean, "requeued_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .eq("id", handback_id)
        .is_("resolved_at", "null")
        .execute()
    )
    return (res.data or [None])[0]


def answers_for_job(user_id: str, job_id: str) -> dict[str, str]:
    """Answers this user gave for this job, for the filler's retry.

    Read on the screener path before any model call: a question the human has already
    answered by hand must never be re-derived, and never answered differently the
    second time.
    """
    if not job_id:
        return {}
    res = (
        get_supabase()
        .table("handbacks")
        .select("answers")
        .eq("user_id", user_id)
        .eq("job_id", job_id)
        .order("created_at", desc=True)
        .limit(5)
        .execute()
    )
    merged: dict[str, str] = {}
    # Newest first, so an older row never overwrites a fresher answer.
    for row in res.data or []:
        for q, a in (row.get("answers") or {}).items():
            merged.setdefault(str(q), str(a))
    return merged
