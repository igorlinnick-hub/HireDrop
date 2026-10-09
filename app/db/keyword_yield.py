"""How much each search phrase brings that fits — per person, per platform.

The batch judge (/tools/assess-fit-batch) decides a whole results page at once, so it knows
how many postings a phrase brought and how many cleared the bar. That count used to be
thrown away: a phrase whose pages were all rejects ("0 fit you, 14 don't") was walked page
after page, each page a minute of judging, and the person never learned why applications
were few.

A phrase is DRY when, under the person's current resume, mode and location, it has had at
least DRY_MIN_PAGES judged pages and DRY_MIN_JUDGED verdicts in the last WINDOW_DAYS with
not one fit. Dry is advice, never a deletion: the walk may skip the phrase for the rest of
a run (it still gets its first page every run), and the dashboard asks the person whether
to refine it. Keywords are NOT part of the fingerprint — replacing one phrase must not wipe
what the others have shown.

Every failure here is swallowed: a missing count must never cost a page or an application.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta

from app.db.client import fetch_paged, get_supabase

WINDOW_DAYS = 7
DRY_MIN_PAGES = 2
DRY_MIN_JUDGED = 20
_MAX_KEYWORD_CHARS = 120


def keyword_key(phrase: str | None) -> str:
    """Same key as the extension's keywordKey(): trimmed, lowercased."""
    return " ".join(str(phrase or "").split()).lower()[:_MAX_KEYWORD_CHARS]


def yield_version(profile: dict | None, resume_text: str | None) -> str:
    profile = profile or {}
    parts = [
        resume_text or "",
        profile.get("apply_mode") or "standard",
        (profile.get("location") or "").strip().lower(),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def record_page(
    user_id: str, keyword: str, platform: str, version: str, judged: int, fits: int
) -> None:
    key = keyword_key(keyword)
    if not key or judged <= 0:
        return
    try:
        get_supabase().table("keyword_yield").insert(
            {
                "user_id": user_id,
                "keyword": key,
                "platform": platform,
                "yield_version": version,
                "judged": judged,
                "fits": fits,
            }
        ).execute()
    except Exception as e:  # noqa: BLE001 — a lost count never costs the page
        print(f"[keyword-yield] record skipped: {e}", file=sys.stderr)


def recent(user_id: str, version: str, platform: str | None = None) -> dict[str, dict]:
    """{keyword: {pages, judged, fits, dry}} for the current fingerprint, last WINDOW_DAYS."""
    since = (datetime.now(UTC) - timedelta(days=WINDOW_DAYS)).isoformat()

    def build(start: int, end: int):
        q = (
            get_supabase()
            .table("keyword_yield")
            .select("keyword, judged, fits")
            .eq("user_id", user_id)
            .eq("yield_version", version)
            .gte("created_at", since)
        )
        if platform:
            q = q.eq("platform", platform)
        return q.order("id").range(start, end)

    try:
        rows = fetch_paged(build, limit=20_000)
    except Exception as e:  # noqa: BLE001 — no stats reads as "nothing known", never as dry
        print(f"[keyword-yield] read skipped: {e}", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(r["keyword"], {"pages": 0, "judged": 0, "fits": 0})
        s["pages"] += 1
        s["judged"] += int(r.get("judged") or 0)
        s["fits"] += int(r.get("fits") or 0)
    for s in out.values():
        s["dry"] = is_dry(s)
    return out


def is_dry(stats: dict | None) -> bool:
    s = stats or {}
    return (
        s.get("pages", 0) >= DRY_MIN_PAGES
        and s.get("judged", 0) >= DRY_MIN_JUDGED
        and s.get("fits", 0) == 0
    )
