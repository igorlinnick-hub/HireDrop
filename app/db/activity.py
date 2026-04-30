"""Activity log — single Storage owner for activity_log table.

Plan ref: PR 4.2 — central observability. Extension and backend write
events here; the dashboard reads them. Today the only writer is the
chrome extension (background.js best-effort POST). Backend writers can
be added incrementally without changing this module's contract.
"""

from app.db.client import get_supabase

ALLOWED_LEVELS = {"info", "warn", "error"}


def write(
    user_id: str,
    message: str,
    level: str = "info",
    phase: str | None = None,
    trace_id: str | None = None,
    metadata: dict | None = None,
) -> str:
    if level not in ALLOWED_LEVELS:
        level = "info"
    res = (
        get_supabase()
        .table("activity_log")
        .insert(
            {
                "user_id": user_id,
                "level": level,
                "phase": phase,
                "trace_id": trace_id,
                "message": message[:2000],
                "metadata_json": metadata or {},
            }
        )
        .execute()
    )
    return res.data[0]["id"] if res.data else ""


def list_recent(user_id: str, limit: int = 100) -> list[dict]:
    res = (
        get_supabase()
        .table("activity_log")
        .select("id, timestamp, level, phase, message, metadata_json, trace_id")
        .eq("user_id", user_id)
        .order("timestamp", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []
