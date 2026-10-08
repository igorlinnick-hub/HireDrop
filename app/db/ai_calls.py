"""The AI call ledger (migrations/add_ai_calls.sql). Single owner of ai_calls.

modules/ai_meter.py writes one row per Anthropic call; readers take the per-day sums from
the ai_calls_daily view.
"""

from app.db.client import fetch_paged, get_supabase


def insert(row: dict) -> None:
    get_supabase().table("ai_calls").insert(row).execute()


def daily(from_day: str, to_day: str, cap: int = 50_000) -> list[dict]:
    """Per UTC day x account x purpose x model sums, both days inclusive (YYYY-MM-DD)."""

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("ai_calls_daily")
            .select(
                "day, user_id, purpose, model, calls, cost_usd, unpriced_calls, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens"
            )
            .gte("day", from_day)
            .lte("day", to_day)
            # PostgREST pages by offset; a total order keeps pages from overlapping.
            .order("day")
            .order("user_id")
            .order("purpose")
            .order("model")
            .range(start, end)
        )

    return fetch_paged(build, cap)


def first_day() -> str | None:
    """The day the ledger starts — earlier days have no record, not zero spend."""
    res = (
        get_supabase().table("ai_calls").select("created_at").order("created_at").limit(1).execute()
    )
    return (res.data[0]["created_at"] or "")[:10] if res.data else None
