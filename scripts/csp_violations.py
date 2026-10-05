#!/usr/bin/env python3
"""Is the site's Report-Only CSP ready to enforce? Counts `[csp]` lines across
EVERY Railway deployment of the last N days.

`railway logs` reads one deployment (the latest by default), so a plain
`railway logs | grep '[csp]'` misses everything logged before the last deploy —
on 10-05 the only real violation sat in the previous deployment and the plain
grep said "clean". This walks them all.

    python scripts/csp_violations.py          # last 7 days
    python scripts/csp_violations.py --days 2

Exit 0 = no violations from our pages (enforce is safe), 1 = some, 2 = can't read.
The canary `doc=/csp-canary` is a manual pipe check and is not counted.
"""

import argparse
import collections
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(
    r"\[csp\] (directive=\S+ blocked=\S+ doc=\S+(?: src=\S+)?)(?: \(\+(\d+) in the last hour\))?"
)


def _env() -> dict:
    env = dict(os.environ)
    dotenv = ROOT / ".env"
    if "RAILWAY_TOKEN" not in env and dotenv.exists():
        for raw in dotenv.read_text().splitlines():
            if raw.startswith("RAILWAY_TOKEN="):
                env["RAILWAY_TOKEN"] = raw.split("=", 1)[1].strip().strip("'\"")
    return env


def _railway(args: list[str], env: dict) -> str:
    # Fixed executable (the CLI on PATH, as prod-sweep uses it), args built here.
    res = subprocess.run(  # noqa: S603
        ["railway", *args],  # noqa: S607
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout or "").strip()[-300:])
    return res.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    days = ap.parse_args().days
    env = _env()
    since = datetime.now(UTC) - timedelta(days=days)

    try:
        deployments = json.loads(_railway(["deployment", "list", "--json"], env))
    except (RuntimeError, ValueError) as e:
        print(f"can't list deployments: {e}", file=sys.stderr)
        return 2
    # A deployment created before the window may still have served inside it:
    # keep everything newer than the window plus the one that was live at its start.
    deployments.sort(key=lambda d: d["createdAt"], reverse=True)
    picked = []
    for d in deployments:
        picked.append(d)
        if datetime.fromisoformat(d["createdAt"].replace("Z", "+00:00")) < since:
            break

    counts: collections.Counter[str] = collections.Counter()
    canary = 0
    for d in picked:
        try:
            out = _railway(
                ["logs", d["id"], "--since", f"{days * 24}h", "--filter", '"[csp]"'], env
            )
        except RuntimeError as e:
            print(f"can't read deployment {d['id'][:8]}: {e}", file=sys.stderr)
            return 2
        for raw in out.splitlines():
            m = LINE.search(raw)
            if not m:
                continue
            if "doc=/csp-canary" in m.group(1):
                canary += 1
                continue
            counts[m.group(1)] += 1 + int(m.group(2) or 0)

    print(f"{len(picked)} deployments, last {days} days; canary lines: {canary}")
    if not counts:
        print(
            "CLEAN — no violations from our pages; the policy can move to Content-Security-Policy."
        )
        return 0
    for line, n in counts.most_common():
        print(f"{n:6}  {line}")
    print(
        f"NOT CLEAN — {len(counts)} distinct violations; allow-list or fix each before enforcing."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
