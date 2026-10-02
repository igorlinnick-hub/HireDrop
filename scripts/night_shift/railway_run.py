"""Railway entrypoint for the night shift: one walk per container start.

The point of running on Railway (and not from the laptop) is that the walk survives a
closed lid, and that the 428 rate is measured from a datacenter IP — the one a real
night shift will have (docs/handoff/night-shift.md).

Variables (service variables on Railway):
  NS_USER       user uuid (required)
  NS_PLATFORM   greenhouse | ashby | all   (default greenhouse)
  NS_MAX        applications this walk may send (default 1)
  NS_LIVE       "1" = really submit; anything else = dry-run (the executor's default)
  NS_WAIT_MIN   minutes to wait for the user's extension campaign to stop (default 360)

Same queue as the extension: while the user's campaign is running, both could reach for
the same posting. So the walk waits until campaign_states.running is false, and gives up
(sends nothing) if it never stops within NS_WAIT_MIN.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.db.client import get_supabase  # noqa: E402

POLL_SECS = 60


def campaign_running(user_id: str) -> bool:
    rows = (
        get_supabase()
        .table("campaign_states")
        .select("running")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
        .data
    )
    return bool(rows and rows[0].get("running"))


def main() -> int:
    user = os.environ.get("NS_USER", "").strip()
    if not user:
        print("[night-shift] NS_USER is not set — nothing to do", flush=True)
        return 0
    platform = os.environ.get("NS_PLATFORM", "greenhouse").strip() or "greenhouse"
    max_n = int(os.environ.get("NS_MAX", "1") or 1)
    live = os.environ.get("NS_LIVE", "").strip() == "1"
    wait_min = int(os.environ.get("NS_WAIT_MIN", "360") or 360)

    deadline = time.monotonic() + wait_min * 60
    while campaign_running(user):
        if time.monotonic() > deadline:
            print(f"[night-shift] campaign still running after {wait_min} min — not walking", flush=True)
            return 0
        print("[night-shift] user's campaign is running — waiting for it to stop", flush=True)
        time.sleep(POLL_SECS)

    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "executor.py"),
        "--user", user,
        "--platform", platform,
        "--max", str(max_n),
    ]
    if live:
        cmd.append("--live")
    print(f"[night-shift] start: platform={platform} max={max_n} live={live}", flush=True)
    code = subprocess.call(cmd)  # noqa: S603 — argv list, no shell; our own executor.py
    # Exit 0 whatever happened: Railway restarts a failed container, and a restart here
    # would be a second walk the user never asked for.
    print(f"[night-shift] walk finished, executor exit={code}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
