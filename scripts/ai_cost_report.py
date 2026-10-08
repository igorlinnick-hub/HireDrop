#!/usr/bin/env python3
"""What AI costs per application, per day — and whether today needs a person to look.

Reads the AI call ledger (ai_calls, written by modules/ai_meter.py on every Anthropic
call) and the applications, and prints, for the window ending yesterday (UTC):

  * spend, calls, applications and AI $ per application, day by day;
  * spend by purpose (fit_judge, cover_letter, job_score, …) and by model;
  * the accounts that cost the most, and those that cost money with no application;
  * what share of spend is charged to no account, and how much of the prompt came from
    the cache;
  * alert lines (modules/ai_spend.alerts): over the ceiling, a jump against the week
    before, idle spend, missing attribution, unpriced models. Exit 1 when there are any.

The ceiling is ai_spend.CEILING_PER_APPLICATION_USD: the monthly price after the affiliate
share spread over every application the paid cap allows — what keeps the heaviest paying
user from costing more than they pay.

Usage (from the repo root, service_role key in .env):

    .venv/bin/python scripts/ai_cost_report.py [--days 8] [--to YYYY-MM-DD]
    .venv/bin/python scripts/ai_cost_report.py --compare <ISO time of a deploy> [--days 3]
    .venv/bin/python scripts/ai_cost_report.py --reconcile    # needs ANTHROPIC_ADMIN_KEY

--compare takes the same number of whole UTC days before and after a deploy (the deploy
day itself counts on neither side) and prints both, so "did this change make an
application cheaper" is one command, counted by the same formula on both sides.
--reconcile asks the Anthropic Usage & Cost Admin API what the organization was billed per
day and prints it next to the ledger: a bill above the ledger is spend from outside the
product (measurement scripts run with AI_METER=off, tests, other keys) or a call site that
skips the meter; a ledger above the bill is a wrong price. The Admin API exists only for
organization accounts, not individual ones.

--emails resolves account ids to addresses — for a terminal, never for a handoff file.

Exit: 0 nothing to report · 1 alerts printed · 2 the ledger could not be read.
Read-only: SELECTs only (and one GET to the Admin API with --reconcile).
"""

import argparse
import os
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, ".")

import config  # noqa: E402,F401  — loads .env before the client reads the keys
from app.db import ai_calls as ai_calls_db  # noqa: E402
from app.db.client import fetch_paged, get_supabase  # noqa: E402
from modules import ai_spend  # noqa: E402

ADMIN_API = "https://api.anthropic.com/v1/organizations/cost_report"
# Billed spend the ledger does not explain, above this share, is worth a look.
UNMETERED_SHARE = 0.30


def _applications(from_day: str, to_day: str) -> list[dict]:
    end = (date.fromisoformat(to_day) + timedelta(days=1)).isoformat()

    def build(start: int, stop: int):
        return (
            get_supabase()
            .table("applications")
            .select("id, user_id, date_applied")
            .gte("date_applied", from_day)
            .lt("date_applied", end)
            .order("date_applied")
            .order("id")
            .range(start, stop)
        )

    return fetch_paged(build, 200_000)


def _summary(from_day: str, to_day: str, metered_since: str | None) -> dict:
    return ai_spend.summarize(
        ai_calls_db.daily(from_day, to_day),
        _applications(from_day, to_day),
        from_day,
        to_day,
        metered_since,
    )


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:,.4f}" if v < 1 else f"${v:,.2f}"


def _emails() -> dict[str, str]:
    users = get_supabase().auth.admin.list_users(per_page=1000)
    return {u.id: u.email or "" for u in users}


def _who(user_id: str | None, emails: dict[str, str] | None) -> str:
    if user_id is None:
        return "(no account)"
    return emails.get(user_id, user_id[:8]) if emails else str(user_id)[:8]


def print_summary(s: dict, emails: dict[str, str] | None, top: int) -> None:
    print(f"AI spend {s['from_day']} .. {s['to_day']} (UTC days)")
    print(f"{'day':12} {'spend':>10} {'calls':>7} {'apps':>5} {'$/app':>9}")
    for d in s["days"]:
        print(
            f"{d['day']:12} {_money(d['cost']):>10} {d['calls']:>7} {d['applications']:>5} "
            f"{_money(d['per_application']):>9}"
        )
    print(
        f"{'TOTAL':12} {_money(s['cost']):>10} {s['calls']:>7} {s['applications']:>5} "
        f"{_money(s['per_application']):>9}   ceiling {_money(ai_spend.CEILING_PER_APPLICATION_USD)}"
    )
    if s["calls_per_application"] is not None:
        print(f"calls per application: {s['calls_per_application']}")
    if s["cache_read_share"] is not None:
        print(f"prompt tokens served from the cache: {s['cache_read_share']:.1%}")
    print(f"charged to no account: {_money(s['unattributed_cost'])}")

    print("\nby purpose")
    for p in s["by_purpose"]:
        share = p["cost"] / s["cost"] if s["cost"] else 0
        print(f"  {p['purpose']:20} {_money(p['cost']):>10} {p['calls']:>7} calls  {share:5.1%}")
    print("\nby model")
    for m in s["by_model"]:
        print(f"  {m['model']:30} {_money(m['cost']):>10} {m['calls']:>7} calls")
    print(f"\ntop {top} accounts")
    print(f"  {'account':34} {'spend':>10} {'calls':>7} {'apps':>5} {'$/app':>9}")
    for a in s["by_account"][:top]:
        print(
            f"  {_who(a['user_id'], emails):34} {_money(a['cost']):>10} {a['calls']:>7} "
            f"{a['applications']:>5} {_money(a['per_application']):>9}"
        )


def print_compare(before: dict, after: dict) -> None:
    print(f"{'':24} {'before':>14} {'after':>14}")
    print(f"{'window':24} {before['from_day'] + '..':>14} {after['from_day'] + '..':>14}")
    for label, key in (
        ("AI spend", "cost"),
        ("applications", "applications"),
        ("AI $ per application", "per_application"),
        ("calls per application", "calls_per_application"),
    ):
        b, a = before[key], after[key]
        fmt = (
            _money
            if key in ("cost", "per_application")
            else (lambda v: "—" if v is None else str(v))
        )
        print(f"{label:24} {fmt(b):>14} {fmt(a):>14}")
    purposes = {p["purpose"] for p in before["by_purpose"]} | {
        p["purpose"] for p in after["by_purpose"]
    }
    b_cost = {p["purpose"]: p["cost"] for p in before["by_purpose"]}
    a_cost = {p["purpose"]: p["cost"] for p in after["by_purpose"]}
    print("by purpose (spend)")
    for p in sorted(purposes, key=lambda x: -(a_cost.get(x, 0) + b_cost.get(x, 0))):
        print(f"  {p:22} {_money(b_cost.get(p, 0)):>14} {_money(a_cost.get(p, 0)):>14}")


def billed_by_day(from_day: str, to_day: str, key: str) -> dict[str, float]:
    """USD the Admin API cost report bills per UTC day."""
    import httpx

    params = {
        "starting_at": f"{from_day}T00:00:00Z",
        "ending_at": f"{(date.fromisoformat(to_day) + timedelta(days=1)).isoformat()}T00:00:00Z",
        "bucket_width": "1d",
        "limit": 31,
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    out: dict[str, float] = {}
    while True:
        resp = httpx.get(ADMIN_API, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        for bucket in body.get("data") or []:
            # Amounts are decimal strings in cents.
            cents = sum(Decimal(r.get("amount") or "0") for r in bucket.get("results") or [])
            out[bucket["starting_at"][:10]] = float(cents / 100)
        if not body.get("has_more"):
            return out
        params["page"] = body["next_page"]


def print_reconcile(s: dict, billed: dict[str, float]) -> list[str]:
    print("\nledger vs Anthropic bill")
    print(f"{'day':12} {'metered':>10} {'billed':>10} {'gap':>10}")
    out = []
    for d in s["days"]:
        if not d["metered"]:
            continue
        bill = billed.get(d["day"], 0.0)
        gap = bill - d["cost"]
        print(f"{d['day']:12} {_money(d['cost']):>10} {_money(bill):>10} {_money(gap):>10}")
        if bill < ai_spend.IDLE_SPEND_USD or abs(gap) / bill <= UNMETERED_SHARE:
            continue
        if gap > 0:
            out.append(
                f"{d['day']}: {gap / bill:.0%} of the Anthropic bill is not in the ledger "
                f"(${gap:.2f}) — other spend in the organization (scripts, tests, other "
                "keys), or a call site without ai_meter.record"
            )
        else:
            out.append(
                f"{d['day']}: the ledger is {-gap / bill:.0%} above the Anthropic bill — "
                "check ai_meter.PRICES"
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=8)
    ap.add_argument("--to", help="last UTC day to include (default: yesterday)")
    ap.add_argument("--compare", help="ISO time of a deploy: --days before vs --days after")
    ap.add_argument("--reconcile", action="store_true")
    ap.add_argument("--emails", action="store_true")
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args(argv)

    yesterday = datetime.now(UTC).date() - timedelta(days=1)
    to_day = date.fromisoformat(args.to) if args.to else yesterday
    try:
        start = ai_calls_db.first_at()
        if args.compare:
            moment = datetime.fromisoformat(args.compare.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            # The deploy day is half one, half the other: it counts on neither side.
            pivot = moment.astimezone(UTC).date()
            after_end = min(pivot + timedelta(days=args.days), yesterday)
            if after_end <= pivot:
                print("no complete UTC day after that moment yet — run it again tomorrow")
                return 0
            before = _summary(
                (pivot - timedelta(days=args.days)).isoformat(),
                (pivot - timedelta(days=1)).isoformat(),
                start,
            )
            after = _summary((pivot + timedelta(days=1)).isoformat(), after_end.isoformat(), start)
            if start and start[:10] > before["from_day"]:
                print(f"note: the ledger starts {start[:10]}; earlier days have no record\n")
            print_compare(before, after)
            return 0
        from_day = (to_day - timedelta(days=args.days - 1)).isoformat()
        s = _summary(from_day, to_day.isoformat(), start)
    except Exception as e:  # noqa: BLE001 — named and exit 2, so a bot never reads it as "all clear"
        print(f"ledger unreadable: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if start is None:
        print("the ledger is empty — no AI call has been recorded yet")
    elif start[:10] > s["from_day"]:
        print(f"note: the ledger starts {start[:10]}; applications before it are left out\n")
    print_summary(s, _emails() if args.emails else None, args.top)

    found = ai_spend.alerts(s)
    if args.reconcile:
        key = os.getenv("ANTHROPIC_ADMIN_KEY", "")
        if not key:
            print("\n--reconcile needs ANTHROPIC_ADMIN_KEY (console → Settings → Admin keys)")
        else:
            found += print_reconcile(s, billed_by_day(s["from_day"], s["to_day"], key))

    print("\nalerts" if found else "\nno alerts")
    for line in found:
        print(f"  ! {line}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
