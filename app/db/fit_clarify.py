"""Drop's questions about close-call postings and the answers to them (fit_clarifications).

The only writer and reader of the table (migrations/add_fit_clarifications.sql). What to ask
and how an answer reads is decided in modules/fit_clarify.py; this module only stores.

Failures are the caller's to swallow: a question that could not be read or stored is a
question not asked, never a broken dashboard or chat.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.db.client import fetch_paged, get_supabase

TABLE = "fit_clarifications"
# Two questions a day at most: a year of them is well under this.
_MAX_ROWS = 2000


def history(user_id: str) -> list[dict]:
    """Every question put to this person, newest first."""

    def build(start: int, end: int):
        return (
            get_supabase()
            .table(TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .order("id")
            .range(start, end)
        )

    return fetch_paged(build, _MAX_ROWS)


def create(user_id: str, fields: dict) -> dict:
    """Store a question. A posting already asked about raises APIError 23505
    (fit_clarifications_user_job_uidx): two dashboard loads racing to ask store one."""
    res = get_supabase().table(TABLE).insert({**fields, "user_id": user_id}).execute()
    if not res.data:
        raise RuntimeError("fit_clarifications insert returned no row")
    return res.data[0]


def get(user_id: str, question_id: str) -> dict | None:
    res = (
        get_supabase()
        .table(TABLE)
        .select("*")
        .eq("id", question_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def mark_seen(user_id: str, question_id: str) -> bool:
    """The person opened the chat with this question in it. First time only."""
    res = (
        get_supabase()
        .table(TABLE)
        .update({"seen_at": _now()})
        .eq("id", question_id)
        .eq("user_id", user_id)
        .is_("seen_at", "null")
        .execute()
    )
    return bool(res.data)


def record_answer(user_id: str, question_id: str, answer: dict, text: str) -> dict | None:
    """Store the answer once. None = no such open question of this person (answered already,
    or not theirs): the second answer never overwrites the first."""
    res = (
        get_supabase()
        .table(TABLE)
        .update(
            {
                # seen_at stays as the chat stored it: when the person first opened it.
                "answered_at": _now(),
                "rating": answer.get("rating"),
                "thumb": answer.get("thumb"),
                "skipped": bool(answer.get("skipped")),
                "answer_text": text or None,
            }
        )
        .eq("id", question_id)
        .eq("user_id", user_id)
        .is_("answered_at", "null")
        .execute()
    )
    return res.data[0] if res.data else None


def answered(since_iso: str | None = None) -> list[dict]:
    """All answered questions, every person — the measure of the judge's disputed zone
    (scripts/clarify_report.py). Service role only."""

    def build(start: int, end: int):
        q = get_supabase().table(TABLE).select("*").not_.is_("answered_at", "null")
        if since_iso:
            q = q.gte("answered_at", since_iso)
        return q.order("answered_at").order("id").range(start, end)

    return fetch_paged(build, 100_000)


def _now() -> str:
    return datetime.now(UTC).isoformat()
