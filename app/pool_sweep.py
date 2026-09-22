"""Nightly top-up of the job pool — the sweep that runs while nobody is looking.

Every path that filled the pool until now hung off a human being present: the campaign
start, the live board search the extension performs in the user's own browser, and the
empty Tap deck. That is fine for the session you are in and useless for the one you are
about to start — open the dashboard the next morning and you are reading yesterday's
inventory, with the first minutes of the run spent waiting for discovery to catch up.

Three things make this cheap enough to run unattended, and they are the reason it exists
in this shape:

  * COST FOLLOWS NOVELTY, NOT THE CLOCK. discover_ats() is handed every link we already
    hold, so a sweep spends its 160 slots on postings we have never seen. Scoring is the
    only paid step ($0.0019/row, measured 09-15) and it runs on new rows only — once a
    search is saturated the nightly sweep costs nothing but board fetches.
  * THE GATE IS ACTIVITY, NOT EXISTENCE. Only accounts that applied inside
    POOL_SWEEP_ACTIVE_DAYS are swept. Scoring inventory for somebody who left is the one
    way this turns into a bill with nothing on the other side of it.
  * ONE SWEEP PER ACCOUNT PER NIGHT, CLAIMED IN THE DATABASE. The Procfile runs two
    uvicorn workers and each holds its own copy of this loop; an in-process guard cannot
    see the other one. The claim is an activity-log line written BEFORE the sweep starts,
    so the loser of the race finds it and skips.

It reuses _run_ats_discovery — the same function the dashboard and the extension call —
on purpose: "what the nightly sweep collects" and "what a campaign collects" must not be
two different behaviours that drift apart.
"""

import asyncio
import random
import sys
from datetime import UTC, datetime, timedelta

from app.db import activity as activity_db
from app.db import applications as apps_db
from config import (
    POOL_SWEEP_ACTIVE_DAYS,
    POOL_SWEEP_EVERY_HOURS,
    POOL_SWEEP_INTERVAL_SECS,
)

# Activity phase — doubles as the cross-worker claim key (see module docstring).
PHASE = "pool-sweep"


def _claim(user_id: str) -> bool:
    """Reserve tonight's sweep for this user, or report that someone else has it.

    Written before the work, not after: a claim that lands only on success leaves the
    whole sweep duration open for the other worker to start the same one.
    """
    since = (datetime.now(UTC) - timedelta(hours=POOL_SWEEP_EVERY_HOURS)).isoformat()
    if activity_db.has_since(user_id, PHASE, since):
        return False
    activity_db.write(
        user_id,
        "Checked the job boards for new roles matching your search",
        phase=PHASE,
    )
    return True


def scan() -> int:
    """One pass over the active accounts. Returns how many sweeps were started."""
    from app.routers.jobs import _FIND_ATS_IN_PROGRESS, _run_ats_discovery

    try:
        user_ids = apps_db.active_user_ids(POOL_SWEEP_ACTIVE_DAYS)
    except Exception as exc:  # noqa: BLE001
        print(f"[pool-sweep] could not list active users: {exc}", file=sys.stderr)
        return 0

    started = 0
    for user_id in user_ids:
        try:
            # The user may be running a campaign right now — its sweep holds this
            # guard, and a second one would re-fetch and re-score the same boards.
            if user_id in _FIND_ATS_IN_PROGRESS or not _claim(user_id):
                continue
            # Take the guard for ourselves; _run_ats_discovery clears it in its finally.
            _FIND_ATS_IN_PROGRESS.add(user_id)
            # Synchronous on purpose: this is already off the event loop (to_thread) and
            # sweeping accounts one at a time keeps the board fetches from stacking into a
            # burst that looks like an attack from our IP.
            _run_ats_discovery(user_id)
            started += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[pool-sweep] {user_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if started:
        print(f"[pool-sweep] swept {started} of {len(user_ids)} active accounts", file=sys.stderr)
    return started


async def sweep_loop() -> None:
    """Background loop, started from the app lifespan when POOL_SWEEP_ENABLED."""
    # Desynchronize the uvicorn workers so they don't hit the claim in the same second.
    await asyncio.sleep(random.uniform(0, min(300, POOL_SWEEP_INTERVAL_SECS)))  # noqa: S311
    while True:
        try:
            # Supabase calls and board fetches are blocking HTTP — off the event loop.
            await asyncio.to_thread(scan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[pool-sweep] loop error: {type(exc).__name__}: {exc}", file=sys.stderr)
        await asyncio.sleep(POOL_SWEEP_INTERVAL_SECS)
