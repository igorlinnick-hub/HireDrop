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

from postgrest.exceptions import APIError

from app.db.client import fetch_paged, get_supabase

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
    """Record a hand-back. Idempotent per open URL: a job handed back twice (a re-run that
    hit the same wall) updates its open row rather than stacking, so the list counts JOBS
    waiting, not attempts.

    Not an upsert: the uniqueness is a PARTIAL index (`WHERE resolved_at IS NULL`, so a
    resolved job can be handed back again), and Postgres refuses `ON CONFLICT (user_id,
    url)` without that predicate — 42P10 on every call. PostgREST cannot send the
    predicate, so from 09-21 to 09-27 every POST /handbacks returned 500, the extension
    swallowed it, and the table stayed empty while 39 hand-backs sat in the activity log.
    """
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
        # The build that hit the wall, stamped here rather than sent by the client: every
        # build back to 1.8.3 already reports its version on ping, so this needs no
        # extension release. Re-stamped on every repeat hand-back (see open_urls).
        "ext_version": _current_build(user_id)[0],
    }
    try:
        return _add_row(user_id, row)
    except APIError as e:
        # DEPLOY-BEFORE-MIGRATION SAFETY: an unknown column makes PostgREST reject the
        # whole write, and a lost hand-back is worse than an unstamped one.
        if e.code != "PGRST204":
            raise
        row.pop("ext_version")
        return _add_row(user_id, row)


def _add_row(user_id: str, row: dict) -> dict | None:
    updated = _update_open(user_id, row)
    if updated is not None:
        return updated
    try:
        res = get_supabase().table("handbacks").insert(row).execute()
    except APIError as e:
        # Two hand-backs for the same URL raced past the lookup: the index let the first
        # in, so the second is an update of the row it just created.
        if e.code != "23505":
            raise
        return _update_open(user_id, row)
    return (res.data or [None])[0]


def _update_open(user_id: str, row: dict) -> dict | None:
    """Overwrite this user's OPEN row for row["url"]; None when there is none."""
    res = (
        get_supabase()
        .table("handbacks")
        .update(row)
        .eq("user_id", user_id)
        .eq("url", row["url"])
        .is_("resolved_at", "null")
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


def open_urls(user_id: str, cap: int = 5000, waiting_only: bool = False) -> list[str]:
    """The URL of EVERY open hand-back — for a walk that must leave those jobs alone.

    list_open() is the dashboard's read and stops at 100 rows; past that the oldest
    hand-backs fell out of the exclusion and were re-opened every night.

    `waiting_only` leaves out rows the person already answered (`requeued_at` set): those
    are meant to run again, with the answers — and rows an OLDER extension build left
    behind. A hand-back says "this build cannot finish the form"; once the user runs a
    newer build that sentence is unproven. 10-02: 1.8.30 taught the Greenhouse filler
    pay/school/state, and 8 of Igor's GH hand-backs would have waited on him forever
    for questions the extension can now answer. The newer build gets one try: if it
    hands the job back again, add() re-stamps the row and it waits again — no loop.
    """
    version, seen_at = _current_build(user_id) if waiting_only else (None, None)

    def build(start: int, end: int):
        q = (
            get_supabase()
            .table("handbacks")
            .select("url, ext_version, created_at" if waiting_only else "url")
            .eq("user_id", user_id)
            .is_("resolved_at", "null")
        )
        if waiting_only:
            q = q.is_("requeued_at", "null")
        return q.order("created_at", desc=True).order("id").range(start, end)

    rows = fetch_paged(build, cap)
    if waiting_only:
        rows = [r for r in rows if not _left_by_older_build(r, version, seen_at)]
    return [r["url"] for r in rows if r.get("url")]


def _current_build(user_id: str) -> tuple[str | None, str | None]:
    """(version, first seen at) of the extension this user runs now — from the ping.

    (None, None) when unknown or unreadable: nothing then counts as left by an older
    build, so every hand-back keeps waiting exactly as before this rule.
    """
    try:
        res = (
            get_supabase()
            .table("campaign_states")
            .select("ext_version, ext_version_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:  # noqa: BLE001
        return None, None
    row = (res.data or [{}])[0] or {}
    return row.get("ext_version") or None, row.get("ext_version_at") or None


def _when(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None


def _left_by_older_build(row: dict, version: str | None, seen_at: str | None) -> bool:
    if not version:
        return False
    if row.get("ext_version"):
        return row["ext_version"] != version
    # Rows from before the stamp: older iff created before the running build first pinged.
    created, first_seen = _when(row.get("created_at")), _when(seen_at)
    return bool(created and first_seen and created < first_seen)


def resolve_for_posting(user_id: str, url: str) -> int:
    """Close the open hand-backs for this posting once it was actually submitted.

    Until 10-02 only the person could close one ("I finished it"), so a retry by a newer
    build that went through left the job on their to-do list anyway. Matched by posting
    identity: the hand-back URL is a form screen, the saved URL may be the posting.
    """
    from modules.job_identity import job_identity, normalized_link

    def key(link: str) -> str | None:
        return job_identity(link) or normalized_link(link)

    target = key(url) if url else None
    if not target:
        return 0
    rows = fetch_paged(
        lambda start, end: (
            get_supabase()
            .table("handbacks")
            .select("id, url")
            .eq("user_id", user_id)
            .is_("resolved_at", "null")
            .order("id")
            .range(start, end)
        ),
        5000,
    )
    ids = [r["id"] for r in rows if r.get("url") and key(r["url"]) == target]
    if not ids:
        return 0
    get_supabase().table("handbacks").update({"resolved_at": datetime.now(UTC).isoformat()}).eq(
        "user_id", user_id
    ).in_("id", ids).is_("resolved_at", "null").execute()
    return len(ids)


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
