#!/usr/bin/env python3
"""Which discovery sources actually return jobs? — the silent-zero audit, as a command.

A scraper that returns [] looks exactly like a scraper that found nothing. That is how
Google Jobs sat in the platform list as "active" for months, how Glassdoor and Wellfound
stayed registered after their endpoints died, and how the DEFAULT platform (RemoteOK)
returned zero for every multi-word keyword. None of it raised, none of it logged.

Run it after touching any scraper, and whenever "Find Jobs" looks thin:

    .venv/bin/python scripts/audit_discovery_sources.py            # default keyword
    .venv/bin/python scripts/audit_discovery_sources.py "data analyst"

    exit 0  -> every source either returned jobs or carries an unavailable_reason
    exit 1  -> at least one source returned a SILENT zero: fix it, or mark it

Hits the real sites, so it is not part of the CI suite.
"""

import contextlib
import io
import sys
import time

sys.path.insert(0, ".")

from app.routers.jobs import SERVER_SCRAPE_SKIP  # noqa: E402
from modules.platforms.registry import PLATFORMS  # noqa: E402


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "marketing manager"
    print(f"Discovery-source audit — keyword={keyword!r}, location='usa'\n")
    print(f"{'source':14s} {'result':14s} {'sec':>5s}  why")

    silent_zeros = []
    for name, cls in PLATFORMS.items():
        platform = cls()
        if platform.requires_credentials:
            print(f"{name:14s} {'skip':14s} {'-':>5s}  needs stored credentials")
            continue
        if name in SERVER_SCRAPE_SKIP:
            print(f"{name:14s} {'browser-side':14s} {'-':>5s}  {SERVER_SCRAPE_SKIP[name]}")
            continue
        if platform.unavailable_reason:
            print(f"{name:14s} {'marked dead':14s} {'-':>5s}  {platform.unavailable_reason}")
            continue

        started = time.time()
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                jobs = platform.scrape(keywords=[keyword], location="usa", max_results=10)
            found = len(jobs)
        except Exception as exc:
            print(f"{name:14s} {'EXCEPTION':14s} {time.time() - started:5.1f}  {exc}")
            silent_zeros.append(name)
            continue

        elapsed = time.time() - started
        last_log = (captured.getvalue().strip().splitlines() or [""])[-1][:80]
        if found:
            print(f"{name:14s} {f'{found} jobs':14s} {elapsed:5.1f}  ok")
        else:
            print(f"{name:14s} {'ZERO':14s} {elapsed:5.1f}  {last_log or 'no error, no jobs'}")
            silent_zeros.append(name)

    if silent_zeros:
        print(
            f"\nSILENT ZEROS: {', '.join(silent_zeros)}"
            "\nEach one is invisible to the user. Fix the scraper, or set its"
            " unavailable_reason so the router skips it with a reason."
        )
        return 1
    print("\nOK: every source either returned jobs or explains itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
