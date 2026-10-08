"""Storage for modules/personal_facts — one jsonb list on the person's profile row.

A list on the profile, not a table: it is small (≤ 40 entries), always read whole (every
reader wants all of it), and the profile row is already read on every answer and letter.
Read-modify-write races are possible in principle (two tabs answering at once); the
loser's answer is the one lost, and the person sees the list afterwards.

The backend runs as service_role (bypasses RLS) — every query filters user_id itself.
"""

from postgrest.exceptions import APIError

from app.db.client import get_supabase
from modules import personal_facts as pf


class FactsUnavailableError(RuntimeError):
    """The column isn't there yet (migrations/add_personal_facts.sql not applied)."""


def get(user_id: str) -> list[dict]:
    try:
        res = (
            get_supabase()
            .table("profiles")
            .select("personal_facts")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except APIError as e:
        # 42703 = undefined column: deploy before migration reads as "no facts yet".
        if e.code in ("42703", "PGRST204"):
            return []
        raise
    row = (res.data or [{}])[0] or {}
    return pf.clean_facts(row.get("personal_facts"))


def save(user_id: str, facts: list[dict]) -> list[dict]:
    clean = pf.clean_facts(facts)
    try:
        (
            get_supabase()
            .table("profiles")
            .update({"personal_facts": clean})
            .eq("user_id", user_id)
            .execute()
        )
    except APIError as e:
        if e.code in ("42703", "PGRST204"):
            raise FactsUnavailableError("personal_facts column missing") from e
        raise
    return clean


def upsert(user_id: str, fact: dict, replace_ids=()) -> tuple[list[dict], dict]:
    facts, saved = pf.upsert(get(user_id), fact, replace_ids)
    return save(user_id, facts), saved


def remove(user_id: str, fact_id: str) -> list[dict]:
    return save(user_id, pf.remove(get(user_id), fact_id))
