"""The `ad_spend` table — one row per (platform, date, ad), service_role only.

Writers: the Meta Insights sync (app/ads/meta_spend.py), the ads-manager agent's
Meta MCP readings (scripts/ads_spend.py ingest-mcp), the Google Ads Script
through POST /admin/ads/spend, and scripts/ads_spend.py for anything bought by
hand. Reader: the Ads section of the admin board.

Rows are an idempotent cache of what the ad platform reports: a re-sync of the
same day overwrites spend/impressions/clicks (platforms keep revising the last
few days), keyed on the table's unique (platform, date, ad_id).
"""

from datetime import UTC, datetime

from app.db.client import fetch_paged, get_supabase

PLATFORMS = ("meta", "google", "manual")
CONFLICT_KEY = "platform,date,ad_id"
UPSERT_CHUNK = 500


def merge_duplicates(rows: list[dict]) -> list[dict]:
    """Collapse rows that share (platform, date, ad_id) by SUMMING their numbers.

    Postgres refuses an upsert that touches the same key twice in one statement,
    and a platform can legitimately report one ad twice for a day (Google: the
    same creative in two ad groups) — summing is the correct roll-up to our key.
    """
    merged: dict[tuple, dict] = {}
    for row in rows:
        key = (row["platform"], str(row["date"]), row["ad_id"])
        if key not in merged:
            merged[key] = dict(row)
            continue
        acc = merged[key]
        acc["spend_usd"] = round(
            float(acc.get("spend_usd") or 0) + float(row.get("spend_usd") or 0), 2
        )
        for k in ("impressions", "clicks"):
            if row.get(k) is not None:
                acc[k] = int(acc.get(k) or 0) + int(row[k])
    return list(merged.values())


def upsert(rows: list[dict]) -> int:
    """Upsert rows (each carrying platform/date/ad_id). Returns rows written."""
    rows = merge_duplicates(rows)
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i : i + UPSERT_CHUNK]
        # synced_at must move on every write — the board's staleness check reads it.
        now = datetime.now(UTC).isoformat()
        stamped = [{**r, "synced_at": now} for r in chunk]
        get_supabase().table("ad_spend").upsert(stamped, on_conflict=CONFLICT_KEY).execute()
    return len(rows)


def read_all(limit: int = 50000) -> list[dict]:
    """Every spend row, paged past PostgREST's 1000-row cap. At $500/month this
    is a few hundred rows a month; the board slices it by date in Python."""

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("ad_spend")
            .select(
                "date, platform, account_id, campaign_id, campaign_name, adset_id, ad_id, "
                "ad_name, spend_usd, impressions, clicks, source, synced_at"
            )
            .order("date")
            .order("id")
            .range(start, end)
        )

    return fetch_paged(build, limit)


def newest_sync(platform: str) -> dict | None:
    """The newest write for a platform: {"synced_at", "source"} or None. The
    source tells the board which writer is live (Meta: token sync or agent)."""
    res = (
        get_supabase()
        .table("ad_spend")
        .select("synced_at, source")
        .eq("platform", platform)
        .order("synced_at", desc=True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None
