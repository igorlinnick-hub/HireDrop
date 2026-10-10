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
from itertools import groupby

from app.db.client import fetch_paged, get_supabase

WINDOW_DAYS = 7
DRY_MIN_PAGES = 2
DRY_MIN_JUDGED = 20
_MAX_KEYWORD_CHARS = 120
_MAX_PAGE_KEY_CHARS = 40
# The extension sends one results page in several requests, all with the same page key.
# Its key restarts every run, so chunks count as one page only while they arrive close
# together; the same key an hour later is the next run's page.
SAME_PAGE_WITHIN = timedelta(minutes=10)


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
    user_id: str,
    keyword: str,
    platform: str,
    version: str,
    judged: int,
    fits: int,
    page_key: str = "",
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
                "page_key": (page_key or "")[:_MAX_PAGE_KEY_CHARS] or None,
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
            .select("id, created_at, keyword, page_key, judged, fits")
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
    rows.sort(key=lambda r: (r["keyword"], r["id"]))
    for keyword, group in groupby(rows, key=lambda r: r["keyword"]):
        s = out[keyword] = {"pages": 0, "judged": 0, "fits": 0}
        last_at: dict[str, datetime] = {}
        for r in group:
            s["judged"] += int(r.get("judged") or 0)
            s["fits"] += int(r.get("fits") or 0)
            at = _parse_time(r.get("created_at"))
            key = r.get("page_key")
            seen = last_at.get(key) if key else None
            if seen is None or at is None or at - seen > SAME_PAGE_WITHIN:
                s["pages"] += 1
            if key and at is not None:
                last_at[key] = at
        s["dry"] = is_dry(s)
    return out


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None  # an unreadable stamp counts as its own page: never merges evidence


def is_dry(stats: dict | None) -> bool:
    s = stats or {}
    return (
        s.get("pages", 0) >= DRY_MIN_PAGES
        and s.get("judged", 0) >= DRY_MIN_JUDGED
        and s.get("fits", 0) == 0
    )
