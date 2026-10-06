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
  * build_queue() turns verdicts into the queue: below the user's bar is out, everything
    else in ONE order — the freshest posting first (Igor, 09-30: the score is a gate, not a
    rank; the list, auto and tap all go top to bottom in that order);
  * the company cap (Igor, 10-06; 09-30 said 2): one application to a company per 60 days,
    counting applications already sent, open hand-backs, AND what the queue itself would
    send. A posting the person sent back with "Try again" is exempt and goes first.

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

# One application per employer per 60 days (Igor, 10-06; 09-30 said two). An open
# hand-back holds the slot too — see app/routers/jobs.py::_prejudged_queue.
COMPANY_CAP = 1
COMPANY_WINDOW_DAYS = 60
# A staffing agency posts many clients' roles under its own name, so one per 60 days would
# cut unrelated employers — but its recruiter still reads every application, so no
# exemption either (10-06 measure: 0 users had 2+ agency applications, so 3 costs nothing).
AGENCY_CAP = 3

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


# Names that hide the employer: "Confidential" alone was 10 unrelated Indeed employers in
# 60 days (10-06 measure), so one application there blocked the other nine. These keys
# (after suffixes are dropped: "Confidential Company" -> "confidential") are no employer
# at all and are never capped, like an empty company.
_HIDDEN_EMPLOYER_KEYS = {
    "confidential",
    "companyconfidential",
    "confidentialemployer",
    "confidencial",
    "hiring",
    "stealth",
    "stealthstartup",
    "stealthmodestartup",
    "undisclosed",
    "anonymous",
    "private",
    "privatepractice",
    "ourclient",
}

# Staffing agencies: the national names (none in the pool on 10-06, but the pool follows
# the users' trades) plus name markers that caught the local ones that are there
# ("southgeorgiastaffing", "bitsrecruiting", "restorationpersonnelsource", "solutions
# driven on behalf of a client"). A false hit only loosens the cap from 1 to 3.
_AGENCY_KEYS = {
    "roberthalf",
    "jobot",
    "insightglobal",
    "teksystems",
    "kforce",
    "randstad",
    "adecco",
    "manpower",
    "manpowergroup",
    "manpowerengineering",
    "aerotek",
    "kelly",
    "kellyservices",
    "apexsystems",
    "cybercoders",
    "everforthcybercoders",
    "motionrecruitmentpartners",
    "actalent",
    "beaconhillstaffinggroup",
    "creativecircle",
    "lhh",
    "vaco",
    "addisongroup",
    "ayahealthcare",
    "amnhealthcare",
    "brooksource",
    "talentclout",
    "talentrust",
    "trustaff",
    "giftedhealthcare",
    "deltaworkforce",
    "laborservices",
    "expressemployment",
    "expressemploymentprofessionals",
}
_AGENCY_MARKERS = ("staffing", "recruit", "personnel", "onbehalfofaclient", "roberthalf")


def company_cap(key: str) -> int:
    """How many applications this employer key may take in the window."""
    if key in _AGENCY_KEYS or any(m in key for m in _AGENCY_MARKERS):
        return AGENCY_CAP
    return COMPANY_CAP


# Tails an ATS board token glues onto the employer's name ("doordashusa", "grafanalabs").
# Matched on the space-free key, so "Grafana Labs" and "grafanalabs" land on one key.
_BOARD_TAILS = ("careers", "jobs", "labs", "usa", "hq")
# A tail is only cut when this much name is left: "Medusa" must not become "med".
_MIN_STEM = 4


def company_key(name: str | None) -> str:
    """One key per employer across boards: "DoorDash, Inc." on Indeed and "doordashusa"
    (the board token Greenhouse rows carry as company) are the same company to the
    recruiter reading both applications. Spaces are dropped ("Muck Rack" = "Muckrack"),
    because a board token never has them. A false merge only makes the cap stricter.
    A name that hides the employer ("Confidential") gets "" — no key, never capped."""
    tokens = re.findall(r"[a-z0-9]+", (name or "").lower())
    while len(tokens) > 1 and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    key = "".join(tokens)
    if key in _HIDDEN_EMPLOYER_KEYS:
        return ""
    for tail in _BOARD_TAILS:
        if key.endswith(tail) and len(key) - len(tail) >= _MIN_STEM:
            key = key[: -len(tail)]
            break
    # Again after the tail: "Confidential Careers" is no employer either.
    return "" if key in _HIDDEN_EMPLOYER_KEYS else key


def companies_holding_slots(user_id: str) -> list[str]:
    """Every company whose slot is taken for the window: one entry per application and per
    hand-back the person did not send back. The ONE read every apply path counts against —
    the server queue and deck (build_queue) and the live Indeed/ZipRecruiter walks
    (/tools/assess-fit). Two lists here would be two authorities that drift apart.

    Best-effort per source: an unreadable history leaves the in-list cap standing and must
    never stop a run."""
    from app.db import applications as apps_db
    from app.db import handbacks as hb_db

    taken: list[str] = []
    for name, read in (
        ("application", apps_db.companies_applied_since),
        ("hand-back", hb_db.companies_handed_back_since),
    ):
        try:
            taken += read(user_id, COMPANY_WINDOW_DAYS)
        except Exception as e:  # noqa: BLE001
            print(f"[company-cap] {name} history unreadable: {e}", file=sys.stderr)
    return taken


def company_slot_taken(company: str | None, taken: list[str]) -> bool:
    key = company_key(company)
    return bool(key) and sum(1 for c in taken if company_key(c) == key) >= company_cap(key)


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
    if not (resume_text or "").strip():
        # The resume did not load (storage hiccup, nothing uploaded). A verdict reached
        # against "Resume: not provided" is a verdict about nobody — storing it would fill
        # the queue with confident rejections. Leave the rows unjudged; the live judge,
        # which reads the resume again at apply time, decides them as it always has.
        return 0

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
    retried_ids: frozenset | set = frozenset(),
) -> dict:
    """Order already-judged rows into the queue the user sees and the run applies in.

    Below the bar is out — "средними не добиваем". Everything else goes in ONE order, the
    freshest posting first: the score decides whether a posting is in the list, never where
    (Igor, 09-30 — until then "ideal >= 70" rode on top, and a week-old 80 sat above this
    morning's 65 that is likelier to still be open). Rows without a verdict for this profile
    (judge down, Indeed — not prejudged yet) take the same freshness order; the live judge
    decides them at apply time, as before. The company cap runs over that final order, so
    the queue never sends a second application to an employer — whether the first went out
    (or was handed back) last month or sits above it in this list.

    `retried_ids` are rows the person sent back with "Try again" on a hand-back. The UI
    then says "back in the queue", so they skip the cap and go first — the cap exists to
    stop US from hammering an employer, not to overrule the person. Without this a
    company's OTHER open hand-backs held its slot and the retried posting was cut (skeptic
    on #353). A retried row still takes the slot, so nothing UNretried from that company
    rides along; several retried postings at one company all go — each was the person's
    own click. The bar still applies: below it the live judge skips the posting at apply
    time anyway, so letting it in would only cost a judge call every run.
    """
    kept_rows, below_bar = [], 0
    for row in rows:
        if has_current_verdict(row, version) and (row.get("fit_score") or 0) < bar:
            below_bar += 1
        else:
            kept_rows.append(row)
    kept_rows.sort(key=_freshness, reverse=True)
    # Stable sort: retried first, freshness order kept inside each group.
    kept_rows.sort(key=lambda r: r.get("id") not in retried_ids)

    sent = Counter(company_key(c) for c in applied_companies if company_key(c))
    kept, company_capped = [], 0
    for row in kept_rows:
        key = company_key(row.get("company"))
        if key and sent[key] >= company_cap(key) and row.get("id") not in retried_ids:
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
