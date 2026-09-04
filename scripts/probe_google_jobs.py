#!/usr/bin/env python3
"""Is Google Jobs reachable from a server again? — the reopen gate, as a command.

Google stopped answering non-JS HTTP clients: `GET /search?udm=8` returns the "enablejs"
redirect interstitial (200 OK, no job payload), so JobSpy's google scraper returns [] every
time. That is why `GoogleJobsPlatform.unavailable_reason` is set and the source is DEFERRED
in the root STATUS_MATRIX.json.

This script is the only thing allowed to flip that decision. Run it, don't argue about it:

    .venv/bin/python scripts/probe_google_jobs.py

    exit 0  -> Google served the jobs payload: clear unavailable_reason, re-verify with a
               real scrape, and update STATUS_MATRIX.json + docs/handoff/google-jobs.md.
    exit 1  -> still gated (expected). Nothing to do.

Network-dependent on purpose, so it is NOT part of the CI suite.
"""

import re
import sys

from jobspy.google.constant import headers_initial
from jobspy.util import create_session

QUERY = "marketing manager jobs near United States"
# The two markers JobSpy needs: the pagination cursor and the job-payload key.
CURSOR_RE = re.compile(r'data-async-fc="([^"]+)"')
PAYLOAD_KEY = "520084652"


def probe(label: str, cookies: dict | None = None, extra: dict | None = None) -> bool:
    session = create_session(is_tls=False, has_retry=True)
    for key, value in (cookies or {}).items():
        session.cookies.set(key, value, domain=".google.com")
    params = {"q": QUERY, "udm": "8", **(extra or {})}
    try:
        response = session.get(
            "https://www.google.com/search", headers=headers_initial, params=params, timeout=30
        )
    except Exception as exc:  # network flake — report, don't crash
        print(f"  {label:18s} EXC  {type(exc).__name__}: {exc}")
        return False

    body = response.text
    has_cursor = bool(CURSOR_RE.search(body))
    has_payload = PAYLOAD_KEY in body
    js_gate = "enablejs" in body
    print(
        f"  {label:18s} status={response.status_code} len={len(body):7d} "
        f"cursor={has_cursor} payload={has_payload} js_gate={js_gate}"
    )
    return has_cursor or has_payload


def main() -> int:
    print(f"Google Jobs reachability probe — q={QUERY!r}")
    reachable = any(
        [
            probe("plain"),
            probe("consent-cookie", cookies={"SOCS": "CAESHAgBEhIaAB", "CONSENT": "YES+cb"}),
            probe("us-english", cookies={"SOCS": "CAESHAgBEhIaAB"}, extra={"gl": "us", "hl": "en"}),
        ]
    )
    if reachable:
        print("\nOPEN: Google served the jobs payload. Re-run a real scrape before believing it.")
        return 0
    print("\nGATED: JS-only interstitial, no job payload. Google Jobs stays DEFERRED.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
