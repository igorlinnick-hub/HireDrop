"""Activity log — single Storage owner for activity_log table.

Plan ref: PR 4.2 — central observability. Extension and backend write
events here; the dashboard reads them. Today the only writer is the
chrome extension (background.js best-effort POST). Backend writers can
be added incrementally without changing this module's contract.
"""

from datetime import UTC, datetime, timedelta

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


# Message-prefix signatures written by the extension (content.js/background.js). Matching
# on these lets the health summary categorize events without a schema change. Keep in sync
# with the log strings — see ROADMAP_E2E.md P3. (A metadata `type` field is the future-proof
# version; text-match is the cheap first cut.)
def _categorize(msg: str) -> str | None:
    m = (msg or "").lower()
    if "✅ applied" in m or m.startswith("applied"):
        return "applied"
    if "no resume attached" in m:
        return "skipped_no_resume"
    if "resume upload failed" in m or "not reflected in ui" in m:
        return "resume_fail"
    if "api 401" in m or "token stale" in m:
        return "auth_401"
    if "⏭️ skipped (fit" in m or "skipped (fit" in m:
        return "skipped_fit"
    if "captcha" in m:
        return "captcha"
    if "not signed into" in m or "login required" in m:
        return "login_required"
    return None


def summary(
    user_id: str, window_hours: int = 24, cap: int = 2000, since: str | None = None
) -> dict:
    """Health snapshot for the dashboard (ROADMAP_E2E.md P3): turns the raw activity feed
    into at-a-glance counts so a silent failure (auth 401, resume fail, everything skipped)
    becomes visible instead of buried in the log.

    `since` (ISO timestamp, e.g. the campaign's started_at) scopes the counts to the CURRENT
    run instead of a rolling 24h window — so a résumé-fail / fit-skip from a PRIOR run on a
    different platform doesn't leak into this campaign's health chips. Falls back to the
    window if `since` is absent or unparseable."""
    scoped_since = None
    if since:
        try:
            # Validate it's a real ISO timestamp before trusting it in the query.
            datetime.fromisoformat(since.replace("Z", "+00:00"))
            scoped_since = since
        except (ValueError, AttributeError):
            scoped_since = None
    cutoff = scoped_since or (datetime.now(UTC) - timedelta(hours=max(1, window_hours))).isoformat()
    res = (
        get_supabase()
        .table("activity_log")
        .select("timestamp, level, message")
        .eq("user_id", user_id)
        .gte("timestamp", cutoff)
        .order("timestamp", desc=True)
        .limit(cap)
        .execute()
    )
    rows = res.data or []
    by_level = {"info": 0, "warn": 0, "error": 0}
    by_type: dict[str, int] = {}
    last_error_at = None
    last_error_msg = None
    for r in rows:
        lvl = r.get("level") if r.get("level") in by_level else "info"
        by_level[lvl] += 1
        if lvl == "error" and last_error_at is None:
            last_error_at = r.get("timestamp")
            last_error_msg = (r.get("message") or "")[:200]
        cat = _categorize(r.get("message", ""))
        if cat:
            by_type[cat] = by_type.get(cat, 0) + 1
    return {
        "window_hours": window_hours,
        "since": scoped_since,  # non-null when the counts are scoped to a single run
        "total": len(rows),
        "truncated": len(rows) >= cap,
        "by_level": by_level,
        "by_type": by_type,
        "applied": by_type.get("applied", 0),
        "skipped_fit": by_type.get("skipped_fit", 0),
        "skipped_no_resume": by_type.get("skipped_no_resume", 0),
        "resume_fail": by_type.get("resume_fail", 0),
        "auth_401": by_type.get("auth_401", 0),
        "last_error_at": last_error_at,
        "last_error_msg": last_error_msg,
    }


def handback_stats(window_hours: int = 168, cap: int = 5000) -> dict:
    """OPERATOR view (admin-only, ALL users): what the filler is failing on.

    The per-user `unfilledLedger` in chrome.storage answered "which field breaks THIS
    browser" and died on reinstall. This is the same signal aggregated across the
    fleet — the list that decides which deterministic handler gets built next
    (background.js ATS_JOB_FAILED writes the metadata this reads).

    Deliberately NOT exposed to users: a field-frequency table is our engineering
    backlog, not something a job seeker should ever have to look at.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=max(1, window_hours))).isoformat()
    res = (
        get_supabase()
        .table("activity_log")
        .select("timestamp, user_id, message, metadata_json")
        .eq("level", "warn")
        .gte("timestamp", cutoff)
        .order("timestamp", desc=True)
        .limit(cap)
        .execute()
    )
    fields: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    by_user: dict[str, int] = {}
    total = 0
    for r in res.data or []:
        meta = r.get("metadata_json") or {}
        # Only rows the extension tagged as hand-backs — every other warn line
        # (cap hits, save failures) shares the level but not this shape.
        if meta.get("type") != "handback":
            continue
        total += 1
        uid = r.get("user_id") or "?"
        by_user[uid] = by_user.get(uid, 0) + 1
        plat = meta.get("platform") or "unknown"
        by_platform[plat] = by_platform.get(plat, 0) + 1
        for label in meta.get("unfilled") or []:
            if isinstance(label, str) and label:
                fields[label[:80]] = fields.get(label[:80], 0) + 1
    top = sorted(fields.items(), key=lambda kv: kv[1], reverse=True)[:50]
    return {
        "window_hours": window_hours,
        "handbacks": total,
        "affected_users": len(by_user),
        "by_platform": by_platform,
        # Sorted worst-first: the users to look at before they churn quietly.
        "by_user": dict(sorted(by_user.items(), key=lambda kv: kv[1], reverse=True)[:50]),
        "top_fields": [{"label": k, "count": v} for k, v in top],
    }
