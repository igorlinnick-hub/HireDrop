"""Platform selectors as data, not code (Phase 4.1).

Indeed redesigns DOM every 1-3 months. Putting selectors in code means
every redesign blocks on a chrome web store rebuild + propagation. This
table lets us patch selectors via SQL — extension picks them up on the
next 24h cache refresh, no release.
"""

from app.db.client import get_supabase


def get(platform: str) -> dict | None:
    res = (
        get_supabase()
        .table("platform_selectors")
        .select("platform, version, selectors_json, updated_at")
        .eq("platform", platform)
        .execute()
    )
    return res.data[0] if res.data else None
