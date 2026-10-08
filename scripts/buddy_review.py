#!/usr/bin/env python3
"""How people use Drop (the support chat) and where it falls short — read the answers.

The admin board's "Drop" section shows the numbers; this prints the same numbers (one
formula: app/db/buddy_log.summarize) and then the flagged answers IN FULL, because the
point of watching a support bot is reading what it said:

    failed          the answer broke (error / went down a rabbit hole)
    didn't know     sent the person to support or said "not sure" — a missing tool or fact
    asked again     the same person asked about the same thing within 3 minutes
    too long        past 1200 characters (the style asks for 2-4 sentences)
    lookup failed   a tool returned an error mid-answer
    thumbs down     the person pressed 👎

Also: cost (each answer's logged tokens x list price), answer time, which lookups it used,
which cards (remember an answer, open an application, rebuild the resume…) were shown and
pressed, and who hit the 20/day cap.

    .venv/bin/python scripts/buddy_review.py [--days 7] [--email someone@x.com]
                                            [--show 20] [--all]

--all prints every question and answer in the window, not only the flagged ones.
Read-only: SELECTs only, paged (PostgREST caps a plain select at 1000 rows).
"""

import argparse
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.db import buddy_log  # noqa: E402
from app.db.client import get_supabase  # noqa: E402


def user_id_for(email: str) -> str:
    for u in get_supabase().auth.admin.list_users(per_page=1000):
        if (u.email or "").lower() == email.lower():
            return u.id
    sys.exit(f"no user with email {email}")


def _block(label: str, text: str, width: int = 100) -> str:
    body = textwrap.fill(
        " ".join((text or "").split()) or "—", width=width, subsequent_indent="      "
    )
    return f"    {label}: {body}"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--email")
    ap.add_argument("--show", type=int, default=20, help="flagged answers to print in full")
    ap.add_argument("--all", action="store_true", help="print every question and answer")
    args = ap.parse_args()

    since = (datetime.now(UTC) - timedelta(days=args.days)).isoformat()
    uid = user_id_for(args.email) if args.email else None
    turns, feedback, limits = buddy_log.read(since, user_id=uid)
    s = buddy_log.summarize(turns, feedback, limits, flagged_limit=max(args.show, 0))
    n = s["questions"]

    def pct(x: int) -> str:
        return f"{x} ({100 * x / n:.0f}%)" if n else str(x)

    print(f"Drop, last {args.days} days{' — ' + args.email if args.email else ''}")
    print(f"  questions        {n} from {s['askers']} people ({s['per_asker']} each)")
    print(
        f"  spend            ${s['cost_usd']:.2f}  ·  per answer ${s['cost_per_answer'] or 0:.4f}"
    )
    print(f"  answer time      p50 {s['latency_p50_s']}s  ·  p95 {s['latency_p95_s']}s")
    print(f"  failed           {pct(s['failed'])}")
    print(f"  didn't know      {pct(s['didnt_know'])}")
    print(f"  asked again      {pct(s['asked_again'])}")
    print(f"  too long         {pct(s['too_long'])}")
    print(f"  👍 / 👎          {s['thumbs_up']} / {s['thumbs_down']}")
    print(f"  cards            {s['cards_pressed']} pressed of {s['cards_shown']} shown")
    for c in s["cards_by_kind"]:
        print(f"    {c['kind']:<22} {c['pressed']}/{c['shown']}")
    print(f"  daily cap        {s['limit_hitters']} people, {s['limit_hits']} refusals")
    if s["tools"]:
        print("  lookups          " + ", ".join(f"{t['tool']} {t['calls']}" for t in s["tools"]))
    print("  per day          " + ", ".join(f"{d[5:]}: {v}" for d, v in s["by_day"]))

    print(
        f"\nFlagged: {s['flagged_total']}"
        + (f" (showing {len(s['flagged'])})" if s["flagged"] else "")
    )
    for f in s["flagged"]:
        print(
            f"\n  [{str(f['at'])[:16]}] {f['account']} — {f['flags']}  ·  tools: {f['tools'] or '—'}"
        )
        print(_block("Q", f["question"]))
        print(_block("A", f["answer"]))

    if args.all:
        print("\nEvery answer:")
        for t in turns:
            m = t.get("metadata_json") or {}
            print(
                f"\n  [{str(t.get('timestamp'))[:16]}] {(t.get('user_id') or '?')[:8]}"
                f"  ${m.get('cost_usd') or 0:.4f}  {m.get('latency_ms') or '?'}ms"
            )
            print(_block("Q", m.get("question") or ""))
            print(_block("A", m.get("answer") or ""))


if __name__ == "__main__":
    main()
