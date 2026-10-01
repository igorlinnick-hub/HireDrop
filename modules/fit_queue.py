"""Judge the pool BEFORE the run, not during it — the prejudged apply queue.

Until 09-30 the fit judge ran in the user's browser, one posting at a time, at the moment
of applying: open the page, read it, judge, and usually skip. A 30-minute run opened 25
postings and applied to none (09-22); a tester got one application in 20 minutes (09-29).
The judge itself held up (calibration 09-30, scripts/measure_judge_calibration.py: 0 of 20
rejections wrong on Igor's pool) — it ran at the wrong time. Its verdict was also never
stored, so the list the user saw and the order the run applied in were two different
things.

This module moves the verdict up front for the postings the server can read whole
(Greenhouse / Lever / Ashby carry the full description in the pool):

  * judge_pending() judges the rows that have no verdict for the CURRENT profile version
    (ai_fit_judge.verdict_version) and stores score + reason on the row;
  * build_queue() turns verdicts into the queue: below the user's bar is out, "ideal"
    (>= 70) on top, then the rest of what clears the bar, freshest first inside each band;
  * the company cap (Igor, 09-30): at most 2 applications to one company per 60 days,
    counting applications already sent AND what the queue itself would send.

Rows nobody could judge (judge down, budget spent) stay UNJUDGED, never "failed": they
ride at the tail and the live judge at apply time still decides them, exactly as before.
A judge outage must cost us the speed-up, not the user's applications.
"""

import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

IDEAL_SCORE = 70
COMPANY_CAP = 2
COMPANY_WINDOW_DAYS = 60

# Rows being judged right now in THIS process. The background pass after a sweep and the
# campaign's own queue read routinely overlap (the extension reads the queue seconds after
# it starts the sweep); without this both pay for the same posting. Per-process only — the
# other uvicorn worker can still double up, which costs a judge call, never a wrong verdict.
_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()

_COMPANY_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "plc",
    "gmbh",
}


def company_key(name: str | None) -> str:
    """One key per employer across boards: "DoorDash, Inc." on Greenhouse and "DoorDash"
    on Indeed are the same company to the recruiter reading both applications."""
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def has_current_verdict(row: dict, version: str) -> bool:
    return row.get("fit_version") == version and row.get("fit_score") is not None


def _freshness(row: dict) -> str:
    return row.get("date_found") or row.get("created_at") or ""


def judge_pending(
    user_id: str,
    profile: dict,
    rows: list[dict],
    *,
    max_calls: int,
    deadline_s: float,
    workers: int = 6,
    resume_text: str | None = None,
    version: str | None = None,
) -> int:
    """Judge the rows that lack a verdict for this profile version, freshest first.

    Each worker stores its own verdict (app.db.jobs.save_fit_verdict), so a call that
    finishes after the deadline is not wasted — the next queue read finds it. Rows judged
    before the deadline are updated IN PLACE (fit_score / fit_reason / fit_version) so the
    caller can rank them without re-reading the pool. Returns how many were judged in time.

    A fail-closed result (judge unavailable) is NOT stored: that row was never judged,
    and a stored 0 would hide it for good.
    """
    from app.db import jobs as jobs_db
    from modules.ai_cover_letter import resume_text_for
    from modules.ai_fit_judge import assess_fit, verdict_version

    if resume_text is None:
        resume_text = resume_text_for(profile)
    if version is None:
        version = verdict_version(profile, resume_text)

    with _IN_FLIGHT_LOCK:
        pending = sorted(
            (
                r
                for r in rows
                if r.get("id") and not has_current_verdict(r, version) and r["id"] not in _IN_FLIGHT
            ),
            key=_freshness,
            reverse=True,
        )[: max(0, max_calls)]
        _IN_FLIGHT.update(r["id"] for r in pending)
    if not pending:
        return 0

    def judge(row: dict) -> tuple[dict, dict | None]:
        try:
            return _judge_one(row)
        finally:
            with _IN_FLIGHT_LOCK:
                _IN_FLIGHT.discard(row["id"])

    def _judge_one(row: dict) -> tuple[dict, dict | None]:
        try:
            verdict = assess_fit(
                job={
                    "title": row.get("title") or "",
                    "company": row.get("company") or "",
                    "description": row.get("description") or "",
                },
                profile=profile,
                resume_text=resume_text,
            )
        except Exception as e:  # noqa: BLE001 — one bad posting must not stop the batch
            print(f"[fit-queue] judge failed on {row.get('id')}: {e}", file=sys.stderr)
            return row, None
        if not verdict.get("judged") or verdict.get("fail_closed"):
            return row, None
        jobs_db.save_fit_verdict(
            row["id"],
            user_id,
            verdict.get("fit_score") or 0,
            verdict.get("reason") or "",
            verdict.get("judge_model") or "",
            version,
        )
        return row, verdict

    started = time.monotonic()
    queue = iter(pending)
    submitted: set[str] = set()
    pool = ThreadPoolExecutor(max_workers=max(1, workers))

    def submit_next(in_flight: set) -> None:
        row = next(queue, None)
        if row is not None:
            submitted.add(row["id"])
            in_flight.add(pool.submit(judge, row))

    in_flight: set = set()
    for _ in range(min(workers, len(pending))):
        submit_next(in_flight)
    judged = 0
    try:
        while in_flight:
            left = deadline_s - (time.monotonic() - started)
            if left <= 0:
                break
            done, in_flight = wait(in_flight, timeout=left, return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    row, verdict = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"[fit-queue] worker error: {e}", file=sys.stderr)
                    continue
                if verdict is not None:
                    row["fit_score"] = verdict.get("fit_score") or 0
                    row["fit_reason"] = verdict.get("reason") or ""
                    row["fit_version"] = version
                    judged += 1
                if deadline_s - (time.monotonic() - started) > 0:
                    submit_next(in_flight)
    finally:
        # Calls already running finish on their own and store their verdicts — the caller
        # just stops waiting for them. Rows never handed to a worker are released.
        pool.shutdown(wait=False, cancel_futures=True)
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.difference_update(r["id"] for r in pending if r["id"] not in submitted)
    return judged


def build_queue(
    rows: list[dict],
    version: str,
    bar: int,
    applied_companies: list[str],
    limit: int,
) -> dict:
    """Order already-judged rows into the queue the user sees and the run applies in.

    Below the bar is out — "средними не добиваем". Ideal (>= IDEAL_SCORE and >= bar) on
    top, then the rest above the bar; inside each band the freshest posting first. Rows
    still unjudged follow for the live judge to decide. The company cap
    runs over that final order, so the queue never sends a third application to an
    employer — whether the first two went out last month or sit above it in this list.
    """
    passing, unjudged, below_bar = [], [], 0
    for row in rows:
        if not has_current_verdict(row, version):
            unjudged.append(row)
        elif (row.get("fit_score") or 0) >= bar:
            passing.append(row)
        else:
            below_bar += 1

    ideal_at = max(IDEAL_SCORE, bar)
    passing.sort(key=_freshness, reverse=True)
    passing.sort(key=lambda r: 0 if (r.get("fit_score") or 0) >= ideal_at else 1)  # stable
    # The unjudged tail has no verdict yet, only the pool scorer's coarse 0-10 — still the
    # best guess at which of them the live judge will pass, so it leads; date breaks ties.
    unjudged.sort(key=_freshness, reverse=True)
    unjudged.sort(key=lambda r: r.get("score") or 0, reverse=True)  # stable

    sent = Counter(company_key(c) for c in applied_companies if company_key(c))
    kept, company_capped = [], 0
    for row in passing + unjudged:
        key = company_key(row.get("company"))
        if key and sent[key] >= COMPANY_CAP:
            company_capped += 1
            continue
        if key:
            sent[key] += 1
        kept.append(row)

    return {
        "jobs": kept[: max(0, limit)],
        "passing": sum(1 for r in kept if has_current_verdict(r, version)),
        "unjudged": sum(1 for r in kept if not has_current_verdict(r, version)),
        "below_bar": below_bar,
        "company_capped": company_capped,
    }
