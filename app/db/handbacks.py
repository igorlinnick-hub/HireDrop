"""Hand-backs — the applications the filler could not finish, as a live to-do list.

The filler's invariant is "submitted-complete-and-honest OR handed back with a reason".
The hand-back half used to exist only as an activity-log line, which meant it scrolled
away: the one thing in the product that actually needs the user's hands was the thing
hardest to find (Igor, 09-21). A log line also cannot be marked done.

One row per unfinished application. `resolved_at IS NULL` = still waiting. Both surfaces
that show this — the extension popup and the dashboard rail — read THIS, so their counts
cannot disagree.
"""

import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urlparse

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


# --- Identity: one open row per JOB --------------------------------------------------
#
# The open-row uniqueness is (user_id, url), so `url` IS the hand-back's identity. Until
# 10-06 the extension sent the screen it stopped on, and every Indeed application runs
# through the same smartapply step URLs with no job in them
# (…/resume-module/structured-data-review, …/questions-module/questions/1). Each new
# Indeed hand-back therefore overwrote the person's previous one: 15 Indeed jobs handed
# back from 09-28 to 10-05 left 5 rows. The fix keeps the constraint and makes the URL
# mean the posting, so no migration and no deploy order.

_INDEED_POSTING = "https://www.indeed.com/viewjob?jk={}"
_JK = re.compile(r"[a-z0-9]{6,32}")


def _indeed_jk(url: str) -> str | None:
    """The Indeed job key in `url` (jk / vjk / jobKey), or None off Indeed or without one."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return None
    if "indeed.com" not in (parsed.hostname or "").lower():
        return None
    q = parse_qs(parsed.query or "")
    jk = ((q.get("jk") or q.get("vjk") or q.get("jobKey") or [""])[0]).strip().lower()
    return jk if _JK.fullmatch(jk) else None


def _pool_jk(user_id: str, title: str, company: str) -> str | None:
    """The posting an old extension's step-URL hand-back was about, from the person's own
    pool: the walk opened it from there (and /jobs/describe saved it). Only an
    unambiguous match counts — two postings with this title and company (two locations,
    a repost) could be either, and a guessed posting is worse than a search link.
    Measured 10-06 on the 28 Indeed hand-backs since 09-22: 22 unique, 5 ambiguous, 1
    without a title.
    """
    if not title:
        return None
    try:
        q = (
            get_supabase()
            .table("jobs")
            .select("link")
            .eq("user_id", user_id)  # service_role bypasses RLS — this filter is the check
            .eq("title", title)
        )
        if company:
            q = q.eq("company", company)
        rows = q.limit(20).execute().data or []
    except Exception:  # noqa: BLE001 — a failed read falls back to the search link
        return None
    jks = {_indeed_jk(r.get("link") or "") for r in rows} - {None}
    return jks.pop() if len(jks) == 1 else None


def posting_url(user_id: str, job: dict) -> str:
    """The URL a hand-back is stored and deduplicated under — the JOB, not the form screen.

    - Indeed with a job key (ext >= this fix sends the /viewjob URL): one spelling,
      https://www.indeed.com/viewjob?jk=<jk>, however the query was written.
    - Indeed without one (an older extension still sending the step URL): the posting
      from the pool by title + company, else an Indeed search for title + company —
      distinct per job, and a link the person can actually use (the step URL opens
      whatever application Indeed has in session, not this one).
    - Nothing to identify the job by (no title, no company): the URL as sent — those
      rows still collapse into one, as before, but no longer swallow named jobs.
    - Every other board: as sent. Greenhouse/Lever/Ashby form URLs carry the posting;
      ZipRecruiter's differ by query.
    """
    url = (job.get("url") or "").strip()
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return url
    if "indeed.com" not in host:
        return url
    jk = _indeed_jk(url)
    if jk:
        return _INDEED_POSTING.format(jk)
    title = " ".join((job.get("job_title") or "").split())[:300]
    company = " ".join((job.get("company") or "").split())[:200]
    if not title and not company:
        return url
    jk = _pool_jk(user_id, title, company)
    if jk:
        return _INDEED_POSTING.format(jk)
    return "https://www.indeed.com/jobs?" + urlencode({"q": f"{title} {company}".strip()})


def add(user_id: str, job: dict) -> dict | None:
    """Record a hand-back. Idempotent per open posting (posting_url): a job handed back
    twice (a re-run that hit the same wall) updates its open row rather than stacking, so
    the list counts JOBS waiting, not attempts — and two different jobs never share a row.

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
        "url": posting_url(user_id, job)[:1000],
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
        # A hand-back of a re-queued job means the retry hit a wall again: it waits on
        # the person again. Left set, the queue would serve the same wall every run.
        "requeued_at": None,
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
            "id, job_title, company, url, platform, reason, steps_done, created_at, questions, answers, job_id, requeued_at, ext_version"
        )
        .eq("user_id", user_id)
        .is_("resolved_at", "null")
        .order("created_at", desc=True)
        .limit(min(max(limit, 1), 100))
        .execute()
    )
    rows = res.data or []
    # `newer_build`: the extension has updated since this form was handed back, so the
    # filler may now finish it. The dashboard offers "try again" on exactly these rows.
    version, seen_at = _current_build(user_id) if rows else (None, None)
    for r in rows:
        r["newer_build"] = (
            not r.get("requeued_at")
            and requeueable(r)
            and _left_by_older_build(r, version, seen_at)
        )
    return rows


# The ATS walk is a server queue: a re-queued hand-back there is picked up by the next run.
# A native Indeed/ZR hand-back has no queue row to return to (the walk is a live search),
# so offering "try again" on it would promise something nothing does.
_QUEUE_PLATFORMS = ("greenhouse", "lever", "ashby")


def requeueable(row: dict) -> bool:
    return bool(row.get("job_id")) or row.get("platform") in _QUEUE_PLATFORMS


def retry(user_id: str, handback_id: str) -> dict | None:
    """The person sends a hand-back back to the queue without answering anything —
    "the extension updated, let it try again". Same stamp the answers path sets, so the
    queue and the extension's local dedup treat both the same way. None when the row is
    not this user's or is already closed."""
    res = (
        get_supabase()
        .table("handbacks")
        .update({"requeued_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .eq("id", handback_id)
        .is_("resolved_at", "null")
        .execute()
    )
    return (res.data or [None])[0]


def companies_handed_back_since(user_id: str, since_days: int, cap: int = 5000) -> list[str]:
    """Company of every hand-back in the last `since_days`, one entry per row — counted
    against the per-company cap next to real applications (modules/fit_queue.py).

    Without it a company whose form always stalls was never "applied to", so it came back
    every run: DoorDash on Igor's account was opened 4 times in 5 days (10-01 ×2, 10-05 ×2),
    each one a filled-and-abandoned form at the same employer. Rows the person sent back
    (`requeued_at` set: "Try again" / answered questions) do not hold the slot — that retry
    is the person's own call and must reach the queue.
    """
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("handbacks")
            .select("company")
            .eq("user_id", user_id)
            .gte("created_at", since)
            .is_("requeued_at", "null")
            .order("created_at", desc=True)
            .order("id")
            .range(start, end)
        )

    return [r["company"] for r in fetch_paged(build, cap) if r.get("company")]


def open_urls(user_id: str, cap: int = 5000, waiting_only: bool = False) -> list[str]:
    """The URL of EVERY open hand-back — for a walk that must leave those jobs alone.

    list_open() is the dashboard's read and stops at 100 rows; past that the oldest
    hand-backs fell out of the exclusion and were re-opened every night.

    `waiting_only` leaves out rows the person sent back to the queue (`requeued_at` set:
    they answered its questions, or pressed "try again" after an extension update) —
    those are meant to run again.

    A newer build never re-opens a hand-back by itself (Igor, 10-02): the person may have
    finished it by hand without pressing "done", and a second submit to the same posting
    is worse than a form left waiting. The update offers the retry (`newer_build` in
    list_open); the person decides.
    """

    def build(start: int, end: int):
        q = (
            get_supabase()
            .table("handbacks")
            .select("url")
            .eq("user_id", user_id)
            .is_("resolved_at", "null")
        )
        if waiting_only:
            q = q.is_("requeued_at", "null")
        return q.order("created_at", desc=True).order("id").range(start, end)

    return [r["url"] for r in fetch_paged(build, cap) if r.get("url")]


def requeued_urls(user_id: str, cap: int = 5000) -> list[str]:
    """URL of every open hand-back the person sent back to the queue (`requeued_at` set:
    "Try again" or answered questions). The queue puts these postings first and lets them
    past the company cap (modules/fit_queue.py::build_queue) — otherwise the company's
    other hand-backs hold its one slot and "back in the queue" is a lie. A new hand-back
    on the retry clears `requeued_at`, which ends the exemption.
    """

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("handbacks")
            .select("url")
            .eq("user_id", user_id)
            .is_("resolved_at", "null")
            .not_.is_("requeued_at", "null")
            .order("created_at", desc=True)
            .order("id")
            .range(start, end)
        )

    return [r["url"] for r in fetch_paged(build, cap) if r.get("url")]


RETRY_EXEMPT_DAYS = 14


def requeued_companies(user_id: str, cap: int = 5000) -> list[str]:
    """Company of every open hand-back the person sent back with "Try again" — for the
    live judge (/tools/assess-fit), which sees a company name but no URL. A retried ATS
    posting passed the cap in build_queue; without this the judge capped it again at
    apply time because the company's OTHER hand-backs hold the slot (skeptic on #359).

    Only retries the queue actually re-runs (requeueable: an ATS board or a pool row with
    a job_id), and only for RETRY_EXEMPT_DAYS. A native Indeed/ZipRecruiter hand-back
    answered on the dashboard, or a retry the judge skipped, stays open with requeued_at
    set — unbounded, that company would lose the cap on every walk (skeptic r2 on #359).
    """
    from datetime import timedelta

    since = (datetime.now(UTC) - timedelta(days=RETRY_EXEMPT_DAYS)).isoformat()

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("handbacks")
            .select("company, platform, job_id")
            .eq("user_id", user_id)
            .is_("resolved_at", "null")
            .gte("requeued_at", since)
            .order("created_at", desc=True)
            .order("id")
            .range(start, end)
        )

    return [r["company"] for r in fetch_paged(build, cap) if r.get("company") and requeueable(r)]


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
    """Was this hand-back written by a build other than the one running now?"""
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
    identity: an older hand-back URL may be a form screen, the saved URL the posting.
    """
    from modules.job_identity import job_identity, normalized_link

    def key(link: str) -> str | None:
        # Indeed first: a posting saved as ?vjk= (the SERP spelling) is the same job as the
        # hand-back's /viewjob?jk=, and job_identity only reads jk.
        jk = _indeed_jk(link)
        return f"indeed:{jk}" if jk else (job_identity(link) or normalized_link(link))

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
