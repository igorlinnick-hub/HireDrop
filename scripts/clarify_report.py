#!/usr/bin/env python3
"""How often the fit judge agrees with the PERSON in its disputed zone — from Drop's questions.

Drop asks people about postings whose score sat within fit_clarify.BAND of their bar
(modules/fit_clarify.py). Each answer is a person's own verdict on a close call, so this is
the one measure of the judge that is not the judge (or a bigger model) grading itself:

  * agree            — on the list and they say it fits, or left off and they say it doesn't;
  * left off, fits   — a good job the judge cost them (a false rejection);
  * let on, doesn't  — a poor job the judge let through (a false pass);
  * unsure           — a 4 to 6: the person is as torn as the judge was.

By model too (fit_model: the cheap screener decided it, or the escalated judge), which is what
the ai-economics lane needs before narrowing the band the good model covers. A SIGNAL, not a
gate: one or two answers a day from the people who open the dashboard is a small, self-chosen
sample (docs/handoff/drop-actions.md). Read the count before the percentage.

Usage (from the repo root, service_role key in .env):

    .venv/bin/python scripts/clarify_report.py [--days 30]

Exit: 0 printed · 2 the answers could not be read. Read-only: SELECTs only.
"""

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta

sys.path.insert(0, ".")

import config  # noqa: E402,F401  — loads .env before the client reads the keys
from app.db import fit_clarify as clarify_db  # noqa: E402
from modules import fit_clarify  # noqa: E402

COLUMNS = ("answered", "agree", "left off, fits", "let on, doesn't", "unsure")


def summarize(rows: list[dict]) -> dict[str, dict[str, int]]:
    """{"all" | <fit_model>: {column: count}} over answered, non-skipped questions."""
    out: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(COLUMNS, 0))
    for r in rows:
        said = fit_clarify.verdict(r)
        if said is None:
            continue
        for key in ("all", r.get("fit_model") or "unknown"):
            s = out[key]
            s["answered"] += 1
            if said == "unsure":
                s["unsure"] += 1
            elif (said == "fits") == (r.get("side") == "above"):
                s["agree"] += 1
            elif said == "fits":
                s["left off, fits"] += 1
            else:
                s["let on, doesn't"] += 1
    return dict(out)


def render(summary: dict[str, dict[str, int]], skipped: int, people: int, days: int) -> str:
    lines = [
        f"Drop's questions, last {days} days: {summary.get('all', {}).get('answered', 0)} "
        f"answered, {skipped} skipped, {people} people.",
        "",
        f"{'':28}" + "".join(f"{c:>17}" for c in COLUMNS),
    ]
    for key in ["all"] + sorted(k for k in summary if k != "all"):
        s = summary[key]
        decided = s["answered"] - s["unsure"]
        agree = f"{s['agree']} ({s['agree'] * 100 // decided}%)" if decided else "0"
        cells = [str(s["answered"]), agree] + [str(s[c]) for c in COLUMNS[2:]]
        lines.append(f"{key[:28]:28}" + "".join(f"{c:>17}" for c in cells))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    try:
        rows = clarify_db.answered(since)
    except Exception as e:  # noqa: BLE001
        print(f"answers unreadable: {e}", file=sys.stderr)
        return 2
    skipped = sum(1 for r in rows if r.get("skipped"))
    people = len({r["user_id"] for r in rows})
    print(render(summarize(rows), skipped, people, args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
