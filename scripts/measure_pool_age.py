#!/usr/bin/env python3
"""How old is the pool, and what would an age cap actually cost us?

The pool is INSERT-only, so it is an archive: rows never expire and `date_found` is only
a tie-break when two jobs score the same (app/routers/jobs.py::get_deck). A stale posting
with a good score therefore outranks a fresh one with an average score, and the walk
spends a page load on a job that closed weeks ago.

Before capping anything we need two numbers the pool alone can answer:

  1. SUPPLY  — how the still-waiting rows (`new`) spread over age. This is what a cap hides.
  2. COST    — how old postings were when a submit actually WENT THROUGH. If real
               applications all land within N days of harvest, a cap at N costs nothing;
               if a tenth of them land at 60 days, the cap throws away real applications.

Usage (from the repo root, service_role key in .env):

    .venv/bin/python scripts/measure_pool_age.py                # whole install
    .venv/bin/python scripts/measure_pool_age.py --user <uuid>  # one account

Read-only: it runs SELECTs and writes nothing.
"""

import argparse
import sys
from collections import Counter
from datetime import UTC, date, datetime

sys.path.insert(0, ".")

import config  # noqa: E402,F401  — loads .env before the client reads the keys
from app.db.client import fetch_paged, get_supabase  # noqa: E402

# Buckets in days. The last one is open-ended — that tail is the point of the exercise.
BUCKETS = [(0, 7), (7, 14), (14, 30), (30, 60), (60, 90), (90, 10_000)]
CANDIDATE_CAPS = (14, 30, 45, 60, 90)


def _parse_day(value) -> date | None:
    """`date_found` is a date, `date_applied` a timestamptz — both reduced to a day."""
    if not value:
        return None
    text = str(value)
    try:
        if len(text) == 10:
            return date.fromisoformat(text)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _bucket(days: int) -> str:
    for lo, hi in BUCKETS:
        if lo <= days < hi:
            return f"{lo}-{hi}d" if hi < 10_000 else f"{lo}d+"
    return "?"


def _histogram(title: str, ages: list[int], total_label: str) -> None:
    print(f"\n{title}")
    if not ages:
        print("  (no rows)")
        return
    counts = Counter(_bucket(a) for a in ages)
    labels = [f"{lo}-{hi}d" if hi < 10_000 else f"{lo}d+" for lo, hi in BUCKETS]
    widest = max(counts.values())
    for label in labels:
        n = counts.get(label, 0)
        bar = "█" * round(24 * n / widest) if widest else ""
        print(f"  {label:>7s}  {n:6d}  {100 * n / len(ages):5.1f}%  {bar}")
    ages_sorted = sorted(ages)
    median = ages_sorted[len(ages_sorted) // 2]
    p90 = ages_sorted[min(len(ages_sorted) - 1, int(0.9 * len(ages_sorted)))]
    print(
        f"  {len(ages)} {total_label} · median {median}d · p90 {p90}d · oldest {ages_sorted[-1]}d"
    )


def _fetch(table: str, columns: str, user_id: str | None, order_col: str) -> list[dict]:
    def build(start: int, end: int):
        q = get_supabase().table(table).select(columns)
        if user_id:
            q = q.eq("user_id", user_id)
        # Secondary order by id: PostgREST paging reshuffles ties otherwise and rows
        # get read twice or not at all (#206).
        return q.order(order_col, desc=True).order("id").range(start, end)

    return fetch_paged(build, 200_000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="restrict to one user_id (default: every account)")
    args = ap.parse_args()

    today = datetime.now(UTC).date()

    jobs = _fetch("jobs", "id, status, platform, date_found", args.user, "date_found")
    waiting = [j for j in jobs if (j.get("status") or "new") == "new"]
    ages = [(today - d).days for j in waiting if (d := _parse_day(j.get("date_found")))]

    scope = f"user {args.user}" if args.user else "all accounts"
    print(f"POOL AGE — {scope}, {today.isoformat()}")
    print(f"  {len(jobs)} rows total · {len(waiting)} still waiting (`new`) · {len(ages)} dated")
    _histogram("SUPPLY — rows still waiting in the pool, by age", ages, "waiting rows")

    by_platform: dict[str, list[int]] = {}
    for j in waiting:
        d = _parse_day(j.get("date_found"))
        if d:
            by_platform.setdefault(j.get("platform") or "?", []).append((today - d).days)
    print("\n  by platform (median age of what's waiting):")
    for platform, vals in sorted(by_platform.items(), key=lambda kv: -len(kv[1])):
        vals.sort()
        print(f"    {platform:14s} {len(vals):6d} rows · median {vals[len(vals) // 2]}d")

    # COST — how stale a posting was when a real submit went through. Joined in memory on
    # job_id; applications that outlived their job row simply don't contribute.
    apps = _fetch("applications", "id, job_id, date_applied, status", args.user, "date_applied")
    found_by_id = {j["id"]: _parse_day(j.get("date_found")) for j in jobs}
    lags = []
    for a in apps:
        applied = _parse_day(a.get("date_applied"))
        found = found_by_id.get(a.get("job_id"))
        if applied and found:
            lags.append(max(0, (applied - found).days))
    print(f"\n  {len(apps)} applications · {len(lags)} joined to a pool row")
    _histogram("COST — posting age at the moment we applied", lags, "applications")

    if lags:
        print("\nWHAT EACH CAP WOULD HAVE COST (applications it would have blocked):")
        for cap in CANDIDATE_CAPS:
            blocked = sum(1 for lag in lags if lag > cap)
            hidden = sum(1 for a in ages if a > cap)
            print(
                f"  cap {cap:3d}d  →  blocks {blocked:5d} real applications "
                f"({100 * blocked / len(lags):4.1f}%) · hides {hidden} waiting rows "
                f"({100 * hidden / max(1, len(ages)):4.1f}% of the pool)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
