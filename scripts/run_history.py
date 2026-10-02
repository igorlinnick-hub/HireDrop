#!/usr/bin/env python3
"""Per-run history: the /tools/run-report numbers for EVERY past run, side by side.

/tools/run-report answers "what did my CURRENT run produce" — it counts from the run's
start to now. Comparing runs (before vs after a change, e.g. the prejudged queue of 09-30)
needs each past run closed at its own end. This script cuts the activity log into runs at
idle gaps and calls the SAME activity.run_report() on each [start, end] — one formula for
"before" and "after", so the numbers compare.

    jobflow/.venv/bin/python scripts/run_history.py --email igor.linnick@gmail.com --days 14
    jobflow/.venv/bin/python scripts/run_history.py --user <uuid> --gap-min 20 --min-opened 1

A "run" = log lines with no gap longer than --gap-min minutes. Runs that opened nothing
(dashboard noise, a sweep alone) are hidden unless --min-opened 0.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.db.activity import run_report  # noqa: E402
from app.db.client import fetch_paged, get_supabase  # noqa: E402


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def user_id_for(email: str) -> str:
    for u in get_supabase().auth.admin.list_users(per_page=1000):
        if (u.email or "").lower() == email.lower():
            return u.id
    sys.exit(f"no user with email {email}")


def runs(user_id: str, days: int, gap_min: int) -> list[tuple[str, str]]:
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()

    def build(start: int, end: int):
        return (
            get_supabase()
            .table("activity_log")
            .select("timestamp")
            .eq("user_id", user_id)
            .gte("timestamp", cutoff)
            .order("timestamp")
            .order("id")
            .range(start, end)
        )

    stamps = [r["timestamp"] for r in fetch_paged(build, 200_000) if r.get("timestamp")]
    out: list[tuple[str, str]] = []
    for ts in stamps:
        if out and _ts(ts) - _ts(out[-1][1]) <= timedelta(minutes=gap_min):
            out[-1] = (out[-1][0], ts)
        else:
            out.append((ts, ts))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--email")
    who.add_argument("--user")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--gap-min", type=int, default=20)
    ap.add_argument("--min-opened", type=int, default=1)
    # One exact window instead of gap-cut runs. Gaps glue a measured run to every start
    # and probe around it: 10-02 a 25-min GH run read as 82 min / 41 min per application
    # because seven aborted starts since 00:10 rode along with it.
    ap.add_argument("--since", help="UTC ISO start of ONE run to report (with --until)")
    ap.add_argument("--until", help="UTC ISO end of that run")
    a = ap.parse_args()
    if bool(a.since) != bool(a.until):
        ap.error("--since and --until go together")

    uid = a.user or user_id_for(a.email)
    print(
        f"{'start (UTC)':16}  {'min':>4} {'opened':>6} {'applied':>7} {'min/app':>7} {'app/h':>5}  top losses"
    )
    windows = [(a.since, a.until)] if a.since else runs(uid, a.days, a.gap_min)
    for start, end in windows:
        r = run_report(uid, since=start, until=end)
        if not a.since and r["opened"] < a.min_opened and r["applied"] == 0:
            continue
        losses = ", ".join(
            f"{k} {v}" for k, v in sorted(r["losses"].items(), key=lambda kv: -kv[1])[:2]
        )
        mpa = r["minutes_per_application"]
        aph = r["applications_per_hour"]
        print(
            f"{start[:16]:16}  {r['minutes']:>4} {r['opened']:>6} {r['applied']:>7}"
            f" {('—' if mpa is None else mpa):>7} {('—' if aph is None else aph):>5}  {losses}"
        )


if __name__ == "__main__":
    main()
