#!/usr/bin/env python3
"""Does the cover letter we pay for actually reach the employer?

We generate a letter on EVERY application (three paths in content.js), store it on the
applications row, and show it in History as part of what we sent. That last claim is the
one nobody had measured — and the run log can answer it, because the filler names every
field it filled:

    STEP 1 [Indeed] filled=[resume,text×2] btn="Continue"   ← no `cover` = letter not typed

So there are two different numbers, and the gap between them is the finding:

  GENERATED — applications rows carrying a non-empty cover_letter (what History shows).
  DELIVERED — form steps that actually typed it (`filled=[…cover…]`), plus, for Indeed,
              whether its apply wizard ever even OPENED a cover-letter module.

Indeed's wizard is modular (`/beta/indeedapply/form/<module>`), and each FORM DIAG line
records the module — so "the employer never asked for a letter" is checkable rather than
assumed.

Usage (from jobflow/, service_role key in .env):

    .venv/bin/python scripts/measure_letter_delivery.py

Read-only: SELECTs only.
"""

import re
import sys
from collections import Counter

sys.path.insert(0, ".")

import config  # noqa: E402,F401  — loads .env before the client reads the keys
from app.db.client import fetch_paged, get_supabase  # noqa: E402

# What one letter costs us, measured 2026-09-21 (scripts/measure_letter_models.py, Sonnet
# on both modes since #225). Kept here so the waste is money, not a row count.
LETTER_COST_USD = 0.00553

MODULE_RE = re.compile(r"path=(\S+)")


def _rows(table: str, columns: str):
    return fetch_paged(
        lambda start, end: (
            get_supabase().table(table).select(columns).order("id").range(start, end)
        ),
        limit=100_000,
    )


def main() -> int:
    apps = _rows("applications", "id, platform, cover_letter")
    logs = _rows("activity_log", "id, message")

    generated = Counter()
    total = Counter()
    for a in apps:
        p = (a.get("platform") or "?").lower()
        total[p] += 1
        if (a.get("cover_letter") or "").strip():
            generated[p] += 1

    # DELIVERED: the filler prints `filled=[…]` for every form step it completes.
    steps = [m for m in (row.get("message") or "" for row in logs) if "filled=[" in m]
    typed = [m for m in steps if "cover" in m.lower().split("filled=[", 1)[-1].split("]")[0]]

    print(
        f"\nGENERATED — letters written and stored ({sum(generated.values())} of {sum(total.values())} applications)"
    )
    for p, n in total.most_common():
        print(f"  {p:<14} {generated[p]:>4} of {n:<4}")

    print(
        f"\nDELIVERED — form steps that actually typed the letter: {len(typed)} of {len(steps)} steps"
    )
    for m in typed[:5]:
        print(f"  · {m[:120]}")

    # Indeed only: which modules its apply wizard ever opened. A cover-letter module that
    # never appears is the difference between "we fail to fill it" and "nobody asked".
    modules = Counter()
    for row in logs:
        msg = row.get("message") or ""
        if msg.startswith("FORM DIAG [indeed]"):
            m = MODULE_RE.search(msg)
            if m:
                modules[m.group(1).rsplit("/form/", 1)[-1].split("/")[0]] += 1
    if modules:
        print(f"\nINDEED WIZARD — modules seen across {sum(modules.values())} form loads")
        for mod, n in modules.most_common():
            mark = "  ← letter step" if "cover" in mod else ""
            print(f"  {mod:<34} {n:>4}{mark}")
        if not any("cover" in mod for mod in modules):
            print("  (no cover-letter module has EVER appeared — Indeed never asked for one)")

    undelivered = sum(generated.values()) - len(typed)
    print(
        f"\nPAID FOR, NOT TYPED: {undelivered} letters ≈ ${undelivered * LETTER_COST_USD:.2f}"
        f" at ${LETTER_COST_USD}/letter"
    )
    print("History shows every one of them as part of the application that was sent.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
