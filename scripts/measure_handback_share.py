#!/usr/bin/env python3
"""How often does an attempt end with the human sent to a form we emptied?

The number that matters here is a SHARE, not a count, and the difference is not cosmetic.
Measured 2026-09-26 the raw count read "39 handbacks in 30 days" — which sounds like an
edge case and invites postponing. The same window held 95 applications, so the real
statement is "29% of finished attempts", and per platform it is worse than that: on
Greenhouse 12 handbacks against 11 applications, on Ashby 4 against 2. A handback is the
MORE common outcome on ATS boards; Indeed's 18% is the exception, not the rule.

Why the share is the honest unit: absolute counts scale with whatever volume the month
happened to have (34 of 36 accounts don't pay, so today's volume is near zero). At the
900/month cap the same behaviour would print ~260 events. The share stays put — so it is
the thing to argue about, and the thing to watch after a fix.

Also run a SHORT window before concluding anything: on 09-26 `--days 7` read 46.2% (36 of
78), meaning 36 of the month's 39 handbacks happened in that last week. The 30-day number
averages a near-idle stretch together with a busy one and understates what the product is
doing NOW. Two windows, or the trend is invisible.

What a handback IS: the extension filled what it could, could not finish, and wrote
"Needs your hands: … Finish it yourself: <url>". As of 09-26 that URL leads to an EMPTY
form, because handBackJob → ATS_JOB_FAILED → navigatePoolNext reuses the same tab and
overwrites it (background.js). Igor's rule of 2026-07-30 says the opposite — the filled
form is supposed to be left for the user — so this script measures the gap between the
promise and the behaviour. Keep running it after the mechanism changes: the share is how
you tell a fix from a feeling.

Usage (from jobflow/, service_role key in .env):

    .venv/bin/python scripts/measure_handback_share.py [--days N]

Read-only: SELECTs only.
"""

import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta

sys.path.insert(0, ".")

import config  # noqa: E402,F401  — loads .env before the client reads the keys
from app.db.client import fetch_paged, get_supabase  # noqa: E402


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _meta(row: dict) -> dict:
    md = row.get("metadata_json")
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            return {}
    return md if isinstance(md, dict) else {}


def main() -> int:
    days = 30
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    since = _since(days)

    # Two reads, both filtered server-side by time so this stays cheap as the log grows.
    # fetch_paged + order("id") because PostgREST silently caps a plain select at 1000 rows.
    logs = fetch_paged(
        lambda s, e: (
            get_supabase()
            .table("activity_log")
            .select("id, message, metadata_json")
            .gte("timestamp", since)
            .order("id")
            .range(s, e)
        ),
        limit=200_000,
    )
    apps = fetch_paged(
        lambda s, e: (
            get_supabase()
            .table("applications")
            .select("id, platform")
            .gte("date_applied", since)
            .order("id")
            .range(s, e)
        ),
        limit=200_000,
    )

    # The durable handback line carries metadata type=handback (background.js mirrors the
    # same labels server-side). The message match is the fallback for rows written before
    # the metadata existed — without it an older window reads as zero handbacks.
    handbacks: Counter = Counter()
    for row in logs:
        md = _meta(row)
        if md.get("type") == "handback" or "Needs your hands" in (row.get("message") or ""):
            handbacks[md.get("platform") or "unknown"] += 1

    applied: Counter = Counter((a.get("platform") or "unknown") for a in apps)
    hb, ap = sum(handbacks.values()), sum(applied.values())

    print(f"window: last {days} days")
    print(f"handbacks: {hb}   applications: {ap}")
    if hb + ap:
        # The denominator prints NEXT TO the share on purpose: the count alone reads as an
        # edge case ("39 a month"), and the share alone invites carrying a percentage with
        # no base. Both together are the only honest sentence.
        print(
            f"SHARE: {100 * hb / (hb + ap):.1f}% of finished attempts end on a form we "
            f"emptied ({hb} of {hb + ap})"
        )
    else:
        print("SHARE: … (no finished attempts in this window — nothing to measure)")

    print("\nby platform — handbacks / applications / share:")
    for p in sorted(set(handbacks) | set(applied), key=lambda k: -handbacks[k]):
        h, a = handbacks[p], applied[p]
        share = f"{100 * h / (h + a):.0f}%" if h + a else "…"
        flag = "  ← handback is the MORE common outcome" if h > a else ""
        print(f"  {p:14} {h:4} / {a:4}   {share:>4}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
