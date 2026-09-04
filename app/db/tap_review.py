"""Tap-review relay — Storage owner for the tap_reviews table.

The desktop extension prepares an application and upserts its review card here;
the phone dashboard reads the card and writes the decision; the extension polls
the decision and performs the actual submit on the computer. One row per user —
the extension never has more than one card pending.
"""

from datetime import UTC, datetime

from app.db.client import get_supabase

# A card older than this has already been auto-skipped by the extension's own
# 30-min review timeout — never show it as actionable.
PENDING_TTL_MIN = 30


def upsert_pending(user_id: str, review_id: str, payload: dict) -> None:
    get_supabase().table("tap_reviews").upsert(
        {
            "user_id": user_id,
            "review_id": review_id,
            "payload": payload,
            "status": "pending",
            "created_at": datetime.now(UTC).isoformat(),
            "decided_at": None,
        }
    ).execute()


def get_current(user_id: str) -> dict | None:
    """The user's current card (any status) or None if absent/stale."""
    res = get_supabase().table("tap_reviews").select("*").eq("user_id", user_id).execute()
    if not res.data:
        return None
    row = res.data[0]
    try:
        created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        age_min = (datetime.now(UTC) - created).total_seconds() / 60
    except (ValueError, KeyError):
        age_min = PENDING_TTL_MIN + 1
    if row.get("status") == "pending" and age_min > PENDING_TTL_MIN:
        return None
    return row


def set_decision(user_id: str, review_id: str, decision: str) -> bool:
    """Record approve/skip for the matching pending card. Returns False if the
    card changed or was already decided — the phone then just refreshes."""
    res = (
        get_supabase()
        .table("tap_reviews")
        .update({"status": decision, "decided_at": datetime.now(UTC).isoformat()})
        .eq("user_id", user_id)
        .eq("review_id", review_id)
        .eq("status", "pending")
        .execute()
    )
    return bool(res.data)
