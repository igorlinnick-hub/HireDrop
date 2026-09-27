#!/usr/bin/env python3
"""Does the pool even KNOW what these jobs pay?

The salary filter is missing from every live path (docs/handoff/salary-filter.md), and the
obvious fix — run `filter_by_salary` at the gates — has a trap we already stepped in with
the city: a filter is only as good as the data it filters. `jobs` has no salary column, so
pay is whatever `modules/salary_filter.parse_salary` can read out of the title and the
scraped description. Where it reads nothing, the job is "salary not listed", and the
`unknown` rule decides everything:

  (a) unknown PASSES  → the filter only rejects pay we can read and that is out of range.
  (b) unknown REJECTS → honest to "$150k minimum", and it deletes most of the deck.

This script measures the three numbers that choose between them, and it never writes:

  1. DEMAND   — how many accounts set a salary bound at all. A filter nobody asked for is
                a theoretical bug; a filter half the users asked for is a money bug.
  2. FILL     — share of pool rows carrying pay we can actually read, by platform. This is
                the ceiling on what ANY salary gate can do.
  3. COST     — of the applications that really went out, how many each variant would have
                blocked, per candidate floor. This is what variant (b) costs in submits.

Usage (from the repo root, service_role key in .env):

    .venv/bin/python scripts/measure_salary_fill.py
    .venv/bin/python scripts/measure_salary_fill.py --user <uuid> --limit 5000
"""

import argparse
import sys
from collections import Counter

sys.path.insert(0, ".")

import config  # noqa: E402,F401  — loads .env before the client reads the keys
from app.db.client import fetch_paged, get_supabase  # noqa: E402
from modules.salary_filter import parse_salary, passes_salary  # noqa: E402

# Floors worth asking about: the round numbers a user actually types into the launch modal.
CANDIDATE_FLOORS = (60_000, 80_000, 100_000, 120_000, 150_000)


def _fetch(table: str, columns: str, user_id: str | None, order_col: str, limit: int) -> list:
    def build(start: int, end: int):
        q = get_supabase().table(table).select(columns)
        if user_id:
            q = q.eq("user_id", user_id)
        return q.order(order_col, desc=True).order("id").range(start, end)

    return fetch_paged(build, limit)


def _pay_text(row: dict) -> str:
    """The exact text passes_salary() reads — same keys, same order, so the fill rate here
    is the fill rate the filter would see, not an optimistic version of it."""
    return " ".join(
        str(row.get(k) or "") for k in ("salary", "compensation", "description", "title")
    )


def _share(n: int, total: int) -> str:
    return f"{100 * n / total:5.1f}%" if total else "    —"


def _bar(n: int, widest: int) -> str:
    return "█" * round(24 * n / widest) if widest else ""


def demand(user_id: str | None) -> None:
    """Who asked for this filter. Counts accounts, not rows."""
    q = (
        get_supabase()
        .table("profiles")
        .select("user_id, salary_min, salary_max, salary_listed_only")
    )
    if user_id:
        q = q.eq("user_id", user_id)
    rows = q.execute().data or []
    with_floor = [r for r in rows if r.get("salary_min")]
    with_ceiling = [r for r in rows if r.get("salary_max")]
    listed_only = [r for r in rows if r.get("salary_listed_only")]
    print("\nDEMAND — accounts that set a salary preference")
    print(
        f"  {len(rows)} profiles · {len(with_floor)} set a floor · {len(with_ceiling)} a ceiling"
        f" · {len(listed_only)} asked for listed-only"
    )
    if with_floor:
        floors = sorted(int(r["salary_min"]) for r in with_floor)
        print(
            f"  floors: {', '.join(f'${f // 1000}k' for f in floors[:12])}"
            f"{' …' if len(floors) > 12 else ''}"
        )


def fill(rows: list) -> tuple[int, int]:
    """Fill rate overall and per platform. Returns (parsed, no_text)."""
    parsed = 0
    no_text = 0
    per_platform: dict[str, Counter] = {}
    for r in rows:
        text = _pay_text(r)
        plat = r.get("platform") or "?"
        c = per_platform.setdefault(plat, Counter())
        c["rows"] += 1
        if not (r.get("description") or "").strip():
            no_text += 1
            c["no_text"] += 1
        if parse_salary(text):
            parsed += 1
            c["parsed"] += 1

    total = len(rows)
    print("\nFILL — pool rows whose pay we can read")
    print(
        f"  {total} rows · {parsed} parseable ({_share(parsed, total)}) ·"
        f" {no_text} have no description at all ({_share(no_text, total)})"
    )
    print("\n  by platform (the native Indeed/ZR path stores the least text):")
    widest = max((c["rows"] for c in per_platform.values()), default=0)
    for plat, c in sorted(per_platform.items(), key=lambda kv: -kv[1]["rows"]):
        print(
            f"    {plat:14s} {c['rows']:6d} rows  parseable {_share(c['parsed'], c['rows'])}"
            f"  no text {_share(c['no_text'], c['rows'])}  {_bar(c['rows'], widest)}"
        )
    return parsed, no_text


def cost(rows: list, apps: list) -> None:
    """What each variant would have blocked — first on the waiting pool (what the deck
    would shrink to), then on applications that really went out (what we'd have lost)."""
    by_id = {r["id"]: r for r in rows}
    applied = [by_id[a["job_id"]] for a in apps if a.get("job_id") in by_id]
    waiting = [r for r in rows if (r.get("status") or "new") == "new"]

    print(
        f"\nCOST — {len(waiting)} waiting rows, {len(applied)} of {len(apps)} applications"
        " joined to a pool row"
    )
    print(f"  {'floor':>8s}   {'(a) unknown passes':>26s}   {'(b) unknown rejects':>26s}")
    print(
        f"  {'':>8s}   {'deck kept':>12s} {'apps kept':>13s}   {'deck kept':>12s} {'apps kept':>13s}"
    )
    for floor in CANDIDATE_FLOORS:
        cells = []
        for listed_only in (False, True):
            deck = sum(1 for r in waiting if passes_salary(r, floor, None, listed_only))
            keep = sum(1 for r in applied if passes_salary(r, floor, None, listed_only))
            cells.append(
                f"{deck:6d} {_share(deck, len(waiting))} {keep:6d} {_share(keep, len(applied))}"
            )
        print(f"  ${floor // 1000:>3d}k     {cells[0]}   {cells[1]}")
    print("\n  Read it as: (a) is the conservative variant — it only drops pay we READ and")
    print("  that misses the floor. (b) is 'listed only': honest to the user's number, and")
    print("  the 'deck kept' column under it is the size of the deck they'd be left with.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="restrict to one user_id (default: every account)")
    ap.add_argument("--limit", type=int, default=20000, help="max pool rows to read")
    args = ap.parse_args()

    scope = f"user {args.user}" if args.user else "all accounts"
    print(f"SALARY FILL — {scope}")

    demand(args.user)
    rows = _fetch(
        "jobs", "id, status, platform, title, description", args.user, "date_found", args.limit
    )
    if not rows:
        print("\n  (no pool rows — nothing to measure)")
        return 0
    fill(rows)
    apps = _fetch("applications", "id, job_id, date_applied", args.user, "date_applied", 20000)
    cost(rows, apps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
