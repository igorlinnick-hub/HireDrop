import os
import sys
import threading
import time
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, HTTPException

from app.db import jobs as jobs_db
from app.deps import get_current_user
from app.schemas import (
    DeadLinkReport,
    FindJobsRequest,
    IngestJobsRequest,
    JobDescriptionRequest,
    JobStatusUpdate,
)
from modules.captcha_profile import TOUCH_RANK, captcha_touch, is_zero_touch

router = APIRouter(tags=["jobs"])

# Per-user cooldown for the heavy ATS discovery sweep (see find_ats_jobs) — repeated
# campaign starts within the window reuse the already-populated pool instead of
# re-scraping ~213 boards. user_id -> (search signature, unix ts of the last real sweep).
#
# Keyed by the SEARCH, not by the user alone: the dashboard writes prefs the moment a chip
# is picked (QuickActions.persistChips), and a warm-up sweep now rides on that write. With
# a time-only cooldown the first chip of a session would spend the window on a half-built
# search — often before any keyword was saved at all — and the complete search, picked
# twenty seconds later, would be refused as "ran recently". A new search is a new question
# and gets its own sweep; the same search re-asked is what the cooldown is for.
FIND_ATS_COOLDOWN_SECS = 10 * 60
# Floor between sweeps even when the search keeps changing — one moving chip must not turn
# into a board-sweep per keystroke. The dashboard debounces on top of this (20s of quiet);
# this is the server-side guarantee, which is the one that holds for every caller.
NEW_SEARCH_MIN_GAP_SECS = 60
_FIND_ATS_LAST_RUN: dict[str, tuple[str, float]] = {}
# Users with a discovery sweep running in a background thread right now — so a second
# request doesn't spawn a duplicate while the first is still working.
_FIND_ATS_IN_PROGRESS: set[str] = set()

# Measured 2026-09-22 with scripts/measure_pool_age.py across the whole install: of the 101
# applications that actually went through, 91% were to postings harvested within 7 days
# (median 0d, p90 5d) — the engine eats fresh inventory. Meanwhile 22% of the waiting pool
# was already older than 30 days, and `date_found` only ever breaks a TIE between equal
# scores (see get_deck's sort), so a stale row with a good score outranks a fresh one with
# an average score and the walk spends a page load on a posting that closed weeks ago.
#
# 45 days is the number the measurement bought, not a guess: it would have blocked ZERO of
# those 101 applications (a 30-day cap would have blocked one) while holding back 21.7% of
# the pool. Rows are never deleted — the archive still answers "have we seen this link"
# and still feeds dedup. The cap only decides what we SHOW and what we APPLY to.
# Re-run the script before moving this number.
MAX_POOL_AGE_DAYS = 45

# The deck's own, stricter cap. A swipe is a PROMISE — "approve this and we'll apply" — and
# the queue always eats freshest-first, so an approved 20-day-old card loses to every new
# arrival and the promise quietly never lands (a welder's 4 approved rows from 09-02 sat
# for three weeks; part of those postings had closed by the time the flag bug freed them).
# Applying is different from swiping: the auto queue may still take a 15-45d row it reaches
# on its own (that costs a page load, not a broken promise), so MAX_POOL_AGE_DAYS stays 45.
#
# 14 is measured, not guessed (scripts/measure_pool_age.py, 2026-09-23, whole install):
# 97.1% of the 102 real applications were to postings ≤14 days old (a 14d cap would have
# blocked 3). It does hide 38% of the waiting pool from the deck — but those are exactly
# the cards whose approval the queue would not honor. Re-run the script before moving it.
DECK_MAX_AGE_DAYS = 14


def fresh_enough(job: dict, max_age_days: int = MAX_POOL_AGE_DAYS) -> bool:
    """Is this posting recent enough to still be worth opening?

    Undated rows PASS — the same "unknown passes" rule the location and job_type filters
    follow: the legacy pool carries rows saved before `date_found` was reliable, and
    emptying the deck to prove a point is the worse failure.
    """
    raw = job.get("date_found")
    if not raw:
        return True
    try:
        found = date.fromisoformat(str(raw)[:10])
    except ValueError:
        return True
    return (datetime.now(UTC).date() - found).days <= max_age_days


def _with_captcha(jobs: list) -> list:
    """Tag each job with its expected captcha burden (single source: captcha_profile) AND
    order zero-touch (no-captcha) jobs first, so the dashboard surfaces quick-apply jobs and
    the campaign applies to them before human-touch ones. Derived live from the persisted
    `platform` column (no schema change). Sort is STABLE, so date_found-desc order (from
    jobs_db.get_jobs) is preserved within each touch band."""
    for j in jobs:
        p = j.get("platform", "")
        j["captcha_touch"] = captcha_touch(p, j.get("company", ""))
        j["zero_touch"] = is_zero_touch(p, j.get("company", ""))
    jobs.sort(key=lambda j: TOUCH_RANK.get(j.get("captcha_touch", "medium"), 1))
    return jobs


# Platforms we deliberately do NOT fetch from our server, each with the reason the user
# sees. Both are discovered IN-BROWSER by the extension during a campaign (per-user home
# IP), so the "compliant by design" claim holds — our server never scrapes them.
# ZipRecruiter is here for a second reason too: its API answers our server with a hard
# Cloudflare 403 ("forbidden aa") for every query and location (measured 2026-09-03), so the
# server-side path could only ever return zero. Its native search-walk in the extension is
# unaffected — ZipRecruiter apply stays VERIFIED.
SERVER_SCRAPE_SKIP = {
    "indeed": "Indeed jobs appear once you start a campaign.",
    "ziprecruiter": "ZipRecruiter jobs appear once you start a campaign.",
}


def select_scrapeable(requested: list[str]) -> tuple[list[str], list[str]]:
    """Split the requested platforms into the ones we actually fetch here, plus the honest
    notes for every one we skip.

    Every skip carries a reason — a source that silently contributes zero jobs is
    indistinguishable from a broken one, which is how both the `jobspy`/`python-jobspy`
    package mix-up (#113) and the dead Google Jobs source stayed invisible in prod.
    """
    from modules.platforms.registry import PLATFORMS

    scrapeable: list[str] = []
    notes: list[str] = []
    for p in requested:
        cls = PLATFORMS.get(p)
        if cls is None or cls.requires_credentials:
            continue
        if p in SERVER_SCRAPE_SKIP:
            notes.append(SERVER_SCRAPE_SKIP[p])
        elif cls.unavailable_reason:
            notes.append(cls.unavailable_reason)
        else:
            scrapeable.append(p)
    return scrapeable, notes


@router.get("/jobs")
def get_jobs(user=Depends(get_current_user)):
    """The dashboard's Job Listings table — display columns only.

    The extension never reads this route (it uses /jobs/ats-queue), so the posting text
    every other caller needs would only be weight on the wire here.
    """
    return _with_captcha(jobs_db.get_jobs_for_listing(user.id))


# Platforms an approved swipe can actually be APPLIED to today. Mirrors TapView's
# TAP_APPLY_PLATFORMS and the extension's buildApprovedAtsQueue() — a card you can swipe
# but nothing can submit is a dead swipe.
TAP_APPLY_PLATFORMS = ("greenhouse", "lever", "indeed", "ashby")


def on_search_filter(jobs: list, profile: dict) -> list:
    """Keep only the pool rows that match the profile's CURRENT search.

    The pool is INSERT-only and never expires, so any raw read of it is an ARCHIVE: every
    job ever harvested under every keyword set the user has tried. Three filters, each
    with its own "unknown passes" rule, documented at the call sites they were born in
    (see get_deck below). Extracted 09-13 because the Tap deck had this and the auto ATS
    queue did not: Igor's 09-13 run walked 15 Oura/Braze ENGINEERING jobs harvested weeks
    earlier under `ai engineer` while his profile read `event manager`, scored them 2-12
    against a bar of 35, applied to nothing, and reported "pool complete" in 2m26s. One
    rule, one place — "what we collect", "what we show" and "what we apply to" cannot
    drift apart again.
    """
    from modules.job_location import location_verdict, names_foreign_country, parse_user_location
    from modules.job_type import matches_job_type
    from modules.platforms.ats_boards import (
        is_generic_talent_pool,
        keyword_match,
        names_other_profession,
    )

    keywords = [k for k in (profile.get("keywords") or []) if (k or "").strip()]
    wanted_type = (profile.get("job_type") or "").strip() or None
    user_loc = parse_user_location(profile.get("location") or "")
    loc_filter_on = bool(user_loc.get("city") or user_loc.get("state_code"))
    # Country gate, independent of the city/state filter above. The coarse "usa"/"remote"
    # enum parses to no city/state, which used to switch the location filter OFF entirely —
    # and a "remote" search surfaced Bengaluru/India rows from the worldwide boards
    # (Igor, 09-21). We serve US job seekers; only the explicit "europe" pick opts out.
    country_gate = _wants_us_jobs(profile)
    return [
        j
        for j in jobs
        if keyword_match(f"{j.get('title', '')} {j.get('location', '')}", keywords)
        # "Join our talent community" is not a vacancy. Gated here as well as at harvest
        # because the pool is INSERT-only: 8 such rows were already banked, 3 of them
        # submitted (live, 09-21), and harvest-side filtering never reaches those.
        and not is_generic_talent_pool(j.get("title") or "")
        # A title that names a trade the user never asked for. Same reason as above:
        # the judge would skip it anyway, but only after opening the page and paying
        # for the read — 164 of Igor's 403 queued rows, 5 of a welder's 7.
        and not names_other_profession(j.get("title") or "", keywords)
        and matches_job_type(j.get("job_type"), wanted_type)
        and (not country_gate or not names_foreign_country(j.get("location")))
        and (not loc_filter_on or location_verdict(j.get("location"), user_loc) != "elsewhere")
    ]


def _wants_us_jobs(profile: dict) -> bool:
    """Every profile except an explicit "europe" pick is a US job seeker — the product,
    the boards we sweep and the resume filler are all US-shaped. One place, so harvest,
    listing, deck and queue can never disagree about who the country gate applies to."""
    return (profile.get("location") or "").strip().lower() != "europe"


@router.get("/jobs/ats-queue")
def get_ats_queue(platform: str, limit: int = 20, user=Depends(get_current_user)):
    """The auto campaign's ATS apply queue — the same pool, cut by the same rule as the deck.

    The extension used to build this from a raw `GET /jobs` and filter only on platform +
    zero_touch, which made every auto run a walk over the archive (see on_search_filter).
    Counts ride along because a queue that silently shrank is indistinguishable from a
    broken one (#113): the campaign logs "N of M" instead of a bare number.

    Ordering mirrors the deck — best fit first, freshest as the tie-break — so the cap
    cuts the tail, not the middle. `already_applied` dedup stays client-side: the
    extension holds the authoritative appliedUrls/appliedJobKeys sets.
    """
    from app.db.profile import get_profile

    pool = [
        j
        for j in jobs_db.get_jobs(user.id)
        if (j.get("status") or "new") == "new"
        and (j.get("link") or j.get("apply_url"))
        and j.get("platform") == platform
        and (platform == "lever" or is_zero_touch(platform, j.get("company", "")))
    ]
    on_search = on_search_filter(pool, get_profile(user.id))
    # Age gate (MAX_POOL_AGE_DAYS): the archive keeps the row, the queue does not open it.
    # Applying to a posting that closed a month ago costs a page load and produces nothing.
    live = [j for j in on_search if fresh_enough(j)]
    live.sort(key=lambda j: j.get("date_found") or "", reverse=True)
    live.sort(key=lambda j: j.get("score") or 0, reverse=True)  # stable: date breaks ties
    return {
        "jobs": _with_captcha(live[: max(1, min(limit, 100))]),
        "pool": len(pool),
        "off_search": len(pool) - len(on_search),
        # Separate from off_search on purpose: "your search matches 40 jobs, 12 of them are
        # too old to still be open" is a different sentence than "40 don't match your
        # search", and a queue that silently shrank must never look like a broken one.
        "stale": len(on_search) - len(live),
    }


@router.get("/jobs/deck")
def get_deck(user=Depends(get_current_user)):
    """The Tap deck: the pool rows that match the CURRENT search, not the pool's history.

    The pool is INSERT-only and never expires, so `GET /jobs` is an archive: every job
    ever harvested under every keyword set the user has tried. The deck used to read that
    archive and filter only on status/link/platform — so changing your keywords on the
    dashboard did nothing to what you swipe. Measured on Igor's account 09-08: 535 rows
    qualified for the deck, 341 of them off-search leftovers from other days' keywords,
    the oldest six weeks old.

    Relevance is decided with the SAME rule that filled the pool (ats_boards.keyword_match),
    so "what we collect for you" and "what we show you" cannot drift apart. No keywords on
    the profile = no filter, exactly as at harvest.

    Returns the cards plus the counts behind them, because a deck that silently shrank is
    indistinguishable from a broken one (#113's lesson): the UI states how many were held
    back and why.
    """
    from app.db.profile import get_profile

    profile = get_profile(user.id)
    keywords = [k for k in (profile.get("keywords") or []) if (k or "").strip()]
    wanted_type = (profile.get("job_type") or "").strip() or None

    swipeable = [
        j
        for j in jobs_db.get_jobs(user.id)
        if (j.get("status") or "new") == "new"
        and (j.get("link") or j.get("apply_url"))
        and j.get("platform") in TAP_APPLY_PLATFORMS
    ]
    # Three filters, not one — all in on_search_filter(), shared with the auto ATS queue.
    # Keywords say WHAT the job is; job_type says on what terms — a contract role is a
    # different answer to "should I apply" than a staff job with the same title, and the
    # picker on the dashboard has been promising this since long before anything wrote the
    # column (see modules/job_type.py). Rows harvested before that write carry no type and
    # pass: emptying the deck to prove a point is the worse failure.
    # Location joined the filters 09-11 (Igor: a Miami profile was swiping Zoox in CA).
    # Three-way verdict from modules/job_location: "elsewhere" is hidden, "unknown"
    # PASSES — the legacy pool carries free-text locations this parser can't always
    # place, and an empty deck is the worse failure. No coordinates exist in the
    # backend, so this is city/state/remote honesty, not a miles radius: the radius
    # picker keeps steering the native searches only.
    on_search = on_search_filter(swipeable, profile)
    # Age gate — the deck's own DECK_MAX_AGE_DAYS (14), stricter than the apply cap (45),
    # because a swipe is a promise the freshest-first queue must be able to keep (see the
    # constant's comment). The tie-break below could never do this job on its own: it only
    # orders cards that already scored the same, so a 60-day-old row with score 8 still sat
    # above a fresh row with score 6.
    live = [j for j in on_search if fresh_enough(j, DECK_MAX_AGE_DAYS)]
    # Best fit first, freshest as the tie-break: `score` is a coarse 0-10 from the Haiku
    # scorer, so whole bands of cards tie and date is what separates a live posting from a
    # six-week-old one. The client interleaves platforms on top of this order.
    live.sort(key=lambda j: j.get("date_found") or "", reverse=True)
    live.sort(key=lambda j: j.get("score") or 0, reverse=True)  # stable: date breaks ties
    return {
        "cards": _with_captcha(live),
        "pool": len(swipeable),
        "off_search": len(swipeable) - len(on_search),
        "stale": len(on_search) - len(live),
        "keywords": keywords,
        "job_type": wanted_type,
        "location": profile.get("location") or "",
        # How much of the pool predates the job_type write and therefore can't be filtered
        # yet. Without this the type filter looks broken while it is merely uninformed.
        "untyped_rows": sum(1 for j in live if not j.get("job_type")),
    }


@router.post("/jobs/find")
def find_jobs(req: FindJobsRequest = None, user=Depends(get_current_user)):
    from app.db.profile import get_profile
    from modules.platforms.registry import PLATFORMS

    profile = get_profile(user.id)
    requested = req.platforms if (req and req.platforms) else profile.get("platforms", ["remoteok"])

    scrapeable, skip_notes = select_scrapeable(requested)
    platforms = [PLATFORMS[p]() for p in scrapeable]
    all_jobs, searched = [], []

    from app.ops_watch import record_scrape

    for platform in platforms:
        found = platform.scrape(
            keywords=profile.get("keywords", []),
            location=profile.get("location", "remote"),
            max_results=25,
        )
        # Feed the silent-zero watch: a live platform whose every scrape returns 0
        # is the #113 signature and must not stay invisible (see app/ops_watch.py).
        record_scrape(platform.name, len(found))
        all_jobs.extend(found)
        searched.append(platform.display_name)

    # So "Find Jobs" is never silently empty: say which sources were skipped and why.
    indeed_note = " ".join(skip_notes)

    if not all_jobs:
        msg = f"No jobs found from {', '.join(searched)}" if searched else "No jobs found"
        if indeed_note:
            msg = indeed_note if not searched else f"{msg}. {indeed_note}"
        return {
            "count": 0,
            "message": msg,
            "platforms": searched,
            "indeed_note": indeed_note,
            "notes": skip_notes,
        }

    # Deduplicate against already-saved jobs, then let Claude score everything.
    # Keyword pre-filter removed: JobSpy platforms already filter via search_term,
    # and Claude Haiku (Haiku cost ~$0.025/200 jobs) is a better semantic judge.
    already_saved = jobs_db.existing_links(user.id, [j["link"] for j in all_jobs])
    new_jobs = [j for j in all_jobs if j["link"] not in already_saved]
    # Country gate at COLLECTION (RemoteOK is a worldwide feed — a "remote" search used to
    # pool Bengaluru/India rows). Before scoring on purpose: never spend model tokens on a
    # job the user cannot take. Same gate as the deck/queue (see _wants_us_jobs).
    from modules.job_location import names_foreign_country

    if _wants_us_jobs(profile):
        new_jobs = [j for j in new_jobs if not names_foreign_country(j.get("location"))]
    # Salary filter runs BEFORE AI scoring (the whole point: don't spend model tokens or
    # an application slot on out-of-range pay). Unlisted salary passes unless the user
    # opted into listed-only — see modules/salary_filter.py.
    from modules.salary_filter import filter_by_salary

    new_jobs, salary_dropped = filter_by_salary(new_jobs, profile)
    resume_text = None
    if new_jobs:
        from modules.ai_cover_letter import resume_text_for
        from modules.ai_job_scorer import score_jobs_batch

        resume_text = resume_text_for(profile)
        new_jobs = score_jobs_batch(new_jobs, profile, resume_text)

    # Resume tailoring is now LAZY (economics #2): it moved out of discovery into
    # the apply-time path — GET /profile/resume/url/best tailors a job on demand the
    # first time the extension fetches its resume to apply. So we pay ~$0.028/tailor
    # only for jobs that reach a real submission, not for every score-≥N job discovered
    # (~70% of which were never applied to). Gating (Premium + Apply-Mode threshold)
    # lives there now. See PLATFORM_AUTOMATION_PLAN.md.

    saved = jobs_db.save_jobs_bulk(user.id, new_jobs)

    message = f"{saved} new jobs saved"
    if salary_dropped:
        message = f"{message} ({salary_dropped} outside your salary range)"
    if indeed_note:
        message = f"{message}. {indeed_note}"
    return {
        "count": saved,
        "message": message,
        "platforms": searched,
        "indeed_note": indeed_note,
        "notes": skip_notes,
        "salary_filtered": salary_dropped,
    }


def _run_ats_discovery(user_id: str) -> None:
    """Heavy ATS discovery — runs in a BACKGROUND thread, OFF the request worker. Fetches
    ~46 boards (now in parallel with a hard deadline in discover_ats), scores, saves into
    the pool, and self-heals thin descriptions. Never raises; always clears the in-progress
    flag. P0: nothing here ever holds an API worker → no more worker-starvation / API 000."""
    from app.db.profile import get_profile
    from data.ats_watchlist import SEED_WATCHLIST
    from modules.ai_cover_letter import resume_text_for
    from modules.ai_job_scorer import score_jobs_batch
    from modules.platforms.ats_boards import discover_ats
    from modules.salary_filter import filter_by_salary

    try:
        profile = get_profile(user_id)
        resume_text = resume_text_for(profile)
        keywords = profile.get("keywords", [])

        # The cap must bound NEW inventory, not everything collected. Measured 09-15: the
        # 213-board watchlist yields 560 postings on Igor's keywords, the sweep returned
        # its top 160, and those 160 were largely the SAME rows every time — so the pool
        # grew only by what churn leaked past a fixed ranking (378 rows/user/30d against a
        # 900 applications/month cap). Handing discovery what we already have lets each
        # sweep spend its 160 slots on postings we've never seen, and the pool walks out to
        # the watchlist's real supply in a few sweeps. Per-sweep cost is unchanged: the
        # slots are the same, and scoring is $0.0019/row (~$0.30 a sweep).
        try:
            known = jobs_db.all_links(user_id)
        except Exception as e:
            print(f"[find-ats bg] pool read failed, sweeping unfiltered: {e}", file=sys.stderr)
            known = set()

        try:
            found = discover_ats(SEED_WATCHLIST, keywords, cap=160, exclude=known)
        except Exception as e:
            print(f"[find-ats bg] discovery failed: {e}", file=sys.stderr)
            found = []
        from app.ops_watch import record_scrape

        record_scrape("ats_boards", len(found))

        # Still ask the DB: `known` is a snapshot, and a concurrent sweep (or a swipe-pool
        # write) can save a link between the read above and here.
        already_saved = jobs_db.existing_links(user_id, [j["link"] for j in found])
        new_jobs = [j for j in found if j["link"] not in already_saved]
        # Country gate at COLLECTION — the 213-board watchlist carries international
        # offices, and their Bengaluru/Toronto/Berlin rows have no business in a US
        # user's pool (or in the scoring bill). Same gate as the deck/queue.
        from modules.job_location import names_foreign_country

        if _wants_us_jobs(profile):
            new_jobs = [j for j in new_jobs if not names_foreign_country(j.get("location"))]
        new_jobs, _salary_dropped = filter_by_salary(new_jobs, profile)
        if new_jobs:
            new_jobs = score_jobs_batch(new_jobs, profile, resume_text)

        jobs_db.save_jobs_bulk(user_id, new_jobs)

        # Self-heal older thin-description rows using the descriptions we just fetched.
        try:
            fresh_desc = {
                f["link"]: f["description"] for f in found if f.get("description") and f.get("link")
            }
            _backfill_thin_ats_scores(user_id, profile, resume_text, cap=40, desc_source=fresh_desc)
        except Exception as e:
            print(f"[find-ats bg] backfill skipped: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[find-ats bg] worker error: {e}", file=sys.stderr)
    finally:
        _FIND_ATS_IN_PROGRESS.discard(user_id)


def _search_signature(profile: dict) -> str:
    """What a sweep is FOR — the search it would collect against.

    Discovery reads the search from the PROFILE, never from the request (see
    _run_ats_discovery), so this is the honest key for "have we already swept this?".
    Keywords are order-insensitive: re-arranging chips is the same question.
    """
    keywords = ",".join(
        sorted(k.strip().lower() for k in (profile.get("keywords") or []) if (k or "").strip())
    )
    location = (profile.get("location") or "").strip().lower()
    job_type = (profile.get("job_type") or "").strip().lower()
    return f"{keywords}|{location}|{job_type}"


@router.post("/jobs/find-ats")
def find_ats_jobs(user=Depends(get_current_user)):
    """Direct-source discovery from the ATS board APIs (Greenhouse/Lever/Ashby).

    Kicks off discovery in a BACKGROUND thread and returns IMMEDIATELY — the request
    worker never blocks on the ~213 board fetches (the old synchronous sweep held a worker
    for minutes and starved the pool → API 000). The pool fills within ~35s; callers read
    it via GET /jobs.

    Two throttles, and which one applies depends on WHAT is being asked:

      * same search as the last sweep  → the 10-minute cooldown. Nothing changed, the pool
        already holds that answer, and re-sweeping would spend its 160 slots on rows we
        just saved.
      * a different search             → a 60-second floor only. A changed search is a new
        question: refusing it is how a warm-up would end up collecting for the keywords the
        user abandoned twenty seconds ago, and then reporting success.

    IMPORTANT for callers: save the prefs FIRST, then call this. The sweep reads keywords,
    location and job_type from the stored profile; called before the write lands it sweeps
    the OLD search and takes the cooldown with it.
    """
    from app.db.profile import get_profile

    now = time.time()
    signature = _search_signature(get_profile(user.id))
    last_signature, last = _FIND_ATS_LAST_RUN.get(user.id, ("", 0.0))
    same_search = signature == last_signature
    window = FIND_ATS_COOLDOWN_SECS if same_search else NEW_SEARCH_MIN_GAP_SECS
    if user.id in _FIND_ATS_IN_PROGRESS or now - last < window:
        return {
            "started": False,
            "cooldown": True,
            "search_changed": not same_search,
            "retry_in_secs": max(0, int(window - (now - last))),
            "message": "ATS discovery is already running / ran recently — using the existing job pool.",
        }
    _FIND_ATS_LAST_RUN[user.id] = (signature, now)
    _FIND_ATS_IN_PROGRESS.add(user.id)
    threading.Thread(target=_run_ats_discovery, args=(user.id,), daemon=True).start()
    return {
        "started": True,
        "search_changed": not same_search,
        "message": "Discovery is running in the background — new jobs land in your pool within ~35s.",
    }


def _backfill_thin_ats_scores(
    user_id: str,
    profile: dict,
    resume_text: str,
    cap: int = 80,
    desc_source: dict[str, str] | None = None,
) -> int:
    """Re-fetch real descriptions for pooled GH/Lever jobs saved with an EMPTY description
    (before the ?content=true fix) and re-score them, so the swipe deck ranks best-fit-first.
    Only touches thin, not-yet-applied rows. Self-terminating: once healed there are no thin
    rows, so later calls are no-ops. `desc_source` (link→description) lets callers reuse
    descriptions they already fetched (e.g. find-ats' discovery pass) to avoid extra board
    calls. Never raises. Returns how many were re-scored."""
    from urllib.parse import urlparse

    from modules.ai_job_scorer import score_job

    pooled = [
        j
        for j in jobs_db.get_jobs(user_id)
        if j.get("platform") in ("greenhouse", "lever")
        and j.get("status") != "applied"  # dedup/applied is the other lane — never touch it
        and len(j.get("description") or "") < 200  # only the thin/empty ones
    ][:cap]
    if not pooled:
        return 0

    def _token(link: str) -> str | None:
        try:
            parts = [p for p in urlparse(link or "").path.split("/") if p]
            return parts[0] if parts else None
        except Exception:
            return None

    if desc_source is not None:
        # Reuse already-fetched descriptions (no extra board calls). Only jobs present in
        # the source get healed; the rest wait for a run whose discovery includes them.
        desc_by_link = desc_source
    else:
        # Fetch fresh descriptions ONCE per (platform, token), then map by apply URL.
        from modules.platforms.ats_boards import fetch_greenhouse, fetch_lever

        desc_by_link = {}
        fetched: set[tuple[str, str]] = set()
        for j in pooled:
            platform, token = j["platform"], _token(j.get("link", ""))
            if not token or (platform, token) in fetched:
                continue
            fetched.add((platform, token))
            try:
                fresh = (
                    fetch_greenhouse(token, None, 200)
                    if platform == "greenhouse"
                    else fetch_lever(token, None, 200)
                )
            except Exception:
                fresh = []
            for f in fresh:
                if f.get("description") and f.get("link"):
                    desc_by_link[f["link"]] = f["description"]

    rescored = 0
    for j in pooled:
        desc = desc_by_link.get(j.get("link"))
        if not desc:
            continue  # job closed / removed from the board — leave the row as-is
        try:
            jobs_db.update_job_description(j["id"], user_id, desc)
            scored = score_job({**j, "description": desc}, profile, resume_text)
            jobs_db.update_job_score(
                j["id"],
                user_id,
                scored["score"],
                scored.get("verdict", ""),
                scored.get("flags", []),
                scored.get("ats_keywords", []),
                scored.get("ats_match_pct", 0),
            )
            rescored += 1
        except Exception as e:
            print(f"[backfill-ats] job {j.get('id')} skipped: {e}", file=sys.stderr)
    return rescored


@router.post("/jobs/backfill-ats-scores")
def backfill_ats_scores(user=Depends(get_current_user)):
    """One-time fix for the swipe deck: GH/Lever jobs saved BEFORE the ?content=true
    change have empty descriptions and near-zero fit scores → best-fit-first is broken.
    Re-fetch their real descriptions, re-score, update in place. Idempotent."""
    from app.db.profile import get_profile
    from modules.ai_cover_letter import resume_text_for

    profile = get_profile(user.id)
    resume_text = resume_text_for(profile)
    rescored = _backfill_thin_ats_scores(user.id, profile, resume_text)
    return {
        "rescored": rescored,
        "message": f"Re-scored {rescored} GH/Lever jobs with real descriptions — the deck now ranks best-fit-first."
        if rescored
        else "No thin-description GH/Lever jobs to backfill.",
    }


# A search-result snippet is thin, but it is a real description. Below this many
# characters we are back to scoring a title with decoration, which #192 showed the
# model rewards rather than penalises. 120 chars ~= one sentence of the posting.
MIN_SCORABLE_DESC = 120


@router.post("/jobs/ingest")
def ingest_jobs(req: IngestJobsRequest, user=Depends(get_current_user)):
    """Harvest-to-pool: the extension saves job cards it SEES in the user's browser
    during a campaign walk (Indeed/ZR search pages). This is the compliant-by-design
    Indeed discovery path — the server never scrapes Indeed (see find_jobs); the
    user's own browser on their home IP does, and the tap deck feeds from this pool.

    INSERT-only: existing links are skipped entirely (an upsert would reset the row's
    status to 'new', resurrecting applied/skipped/approved jobs — the dedup lane).

    Scoring is conditional on there being something to score. Title-only cards stay
    unscored: that is the ~6%-noise trap the GH backfill fixed, and #192 confirmed it
    from the other side — with no description the model scores the title match and
    scores it HIGH. But the promise the old docstring made here ("score stays null
    until a description-bearing pass rescores them") was never kept: no such pass
    exists for Indeed — _backfill_thin_ats_scores only covers greenhouse and lever —
    so 425 rows, a third of the pool and a VERIFIED platform, sat at null forever and
    sorted last.

    So: cards that arrive WITH a description (the search-result snippet the extension
    now sends) get scored like any other job. A snippet is thin, but thin is what the
    deck ranks on, and ranking has to happen BEFORE the user swipes — enriching at
    apply time would deliver a score for a job we have already applied to.
    """
    harvest_platforms = {"indeed", "ziprecruiter"}
    candidates = [
        j
        for j in (req.jobs or [])[:30]  # per-call cap: one search page is ~15 cards
        if j.platform in harvest_platforms and j.link and j.title
    ]
    # Existing links never enter the upsert — that's what keeps this INSERT-only.
    already_saved = jobs_db.existing_links(user.id, [j.link for j in candidates])
    fresh = [j for j in candidates if j.link not in already_saved]

    rows = [
        {
            "title": j.title,
            "company": j.company,
            "link": j.link,
            "status": "new",
            "platform": j.platform,
            "description": j.description,
            "location": j.location,
            "job_type": j.job_type,
        }
        for j in fresh
    ]

    scorable = [r for r in rows if len((r.get("description") or "").strip()) >= MIN_SCORABLE_DESC]
    scored_count = 0
    if scorable:
        # Never let a scoring hiccup cost the harvest: the rows are the point, the
        # score is an improvement. An exception here used to mean the whole page of
        # cards was lost.
        try:
            from app.db.profile import get_profile
            from modules.ai_cover_letter import resume_text_for
            from modules.ai_job_scorer import score_jobs_batch

            profile = get_profile(user.id)
            score_jobs_batch(scorable, profile, resume_text_for(profile))
            scored_count = sum(1 for r in scorable if r.get("score") is not None)
        except Exception as e:
            print(f"[ingest] scoring skipped (rows still saved): {e}", file=sys.stderr)

    saved = jobs_db.save_jobs_bulk(user.id, rows)
    return {
        "saved": saved,
        "skipped_existing": len(candidates) - len(fresh),
        # Honest counters: "saved 15, scored 0" is a snippet-extraction regression in
        # the extension, and it must not look identical to a healthy run.
        "scored": scored_count,
        "unscored_title_only": len(rows) - len(scorable),
    }


# Upper bound on stored posting text. The longest real posting in the pool is ~3400
# chars; 20k leaves room for verbose ATS boards while refusing a page dump.
MAX_DESCRIPTION_CHARS = 20_000
# Below this it isn't a posting, it's a card snippet (Indeed's longest is ~90) — and
# writing it would overwrite good text harvested earlier with a salary string.
MIN_STORABLE_DESC = 300


@router.post("/jobs/describe")
def describe_job(req: JobDescriptionRequest, user=Depends(get_current_user)):
    """Record the posting text the extension is reading on the detail page.

    Indeed never reaches the server (it 403s us), so everything we know about an
    Indeed job came from the search card — a snippet like "From $40,000 a yearFull-time".
    Measured 2026-09-20: 0 of 387 Indeed pool rows held real text. Meanwhile the full
    posting is right there in the user's browser, already parsed for the cover letter.

    Three things downstream read that column and were all being fed the snippet:
    resume tailoring (paid Sonnet to target a salary string), the fit judge, and the
    interview kit — which answers "we don't have the text of this job posting" for
    86% of our applications today.

    Text-only write: never touches `status`, so a job already applied to or skipped
    cannot be resurrected into the walk by describing it.
    """
    text = (req.description or "").strip()[:MAX_DESCRIPTION_CHARS]
    if not req.link or len(text) < MIN_STORABLE_DESC:
        return {"stored": False, "reason": "not enough posting text"}
    job_id = jobs_db.save_description(
        user.id,
        req.link,
        text,
        title=req.title,
        company=req.company,
        platform=req.platform,
    )
    return {"stored": bool(job_id), "job_id": job_id, "chars": len(text)}


@router.patch("/jobs/{job_id}/status")
def patch_job_status(job_id: str, req: JobStatusUpdate, user=Depends(get_current_user)):
    """Record a decision on one pool row — the write behind every Tap swipe.

    404 when nothing matched, instead of the old unconditional {"updated": True}. An
    approve that silently wrote nothing is the worst shape this bug takes: the card flew
    off the deck, the user counts it as queued, and no run will ever pick it up because
    the row never became `approved`. Say so, so the deck can put the card back.
    """
    changed = jobs_db.update_job_status(user.id, job_id, req.status)
    if not changed:
        raise HTTPException(status_code=404, detail="No such job in your pool")
    return {"updated": True, "job_id": job_id, "status": req.status}


@router.post("/jobs/dead-link")
def report_dead_link(req: DeadLinkReport, user=Depends(get_current_user)):
    """The walk opened this posting and the board answered "Not Found" — retire it.

    Without this a dead row stays `new` and comes back on every run: the extension's own
    guard (content.js pageLooksNotFound) keeps the walk moving, but the posting is still
    re-opened, re-loaded and re-skipped forever. Reported by the extension, scoped to the
    caller's own rows, and it only touches postings still waiting in the pool.
    """
    n = jobs_db.mark_dead_link(user.id, req.url)
    return {"retired": n}


@router.post("/jobs/{job_id}/tailor")
def tailor_job(job_id: str, user=Depends(get_current_user)):
    """On-demand tailoring for one job — the dashboard "Tailor for this job" button,
    for discovery/manual applies that never hit the extension's apply-time /best call.
    Same lazy path + gating (Premium + Apply-Mode threshold); idempotent. Returns the
    tailored PDF URL when ready.
    """
    from fastapi.responses import JSONResponse

    from app.db import resume as resume_storage
    from app.routers.profile import _lazy_tailor_for_job

    job = jobs_db.get_job_by_id(user.id, job_id)
    if not job:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    _lazy_tailor_for_job(user, job)
    job = jobs_db.get_job_by_id(user.id, job_id)
    if job and job.get("tailored_resume_pdf_url"):
        return {
            "tailored": True,
            "url": resume_storage.signed_url_from_path(job["tailored_resume_pdf_url"], user.id),
        }
    return {
        "tailored": False,
        "reason": "Not eligible — Premium + strong match required, or no resume uploaded.",
    }
