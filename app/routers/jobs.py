import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends

from app.db import jobs as jobs_db
from app.deps import get_current_user
from app.schemas import FindJobsRequest, IngestJobsRequest, JobStatusUpdate
from modules.captcha_profile import TOUCH_RANK, captcha_touch, is_zero_touch
router = APIRouter(tags=["jobs"])

# Per-user cooldown for the heavy ATS discovery sweep (see find_ats_jobs) — repeated
# campaign starts within the window reuse the already-populated pool instead of
# re-scraping ~46 boards. user_id -> unix ts of the last real sweep.
FIND_ATS_COOLDOWN_SECS = 10 * 60
_FIND_ATS_LAST_RUN: dict[str, float] = {}


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


@router.get("/jobs")
def get_jobs(user=Depends(get_current_user)):
    return _with_captcha(jobs_db.get_jobs(user.id))


@router.post("/jobs/find")
def find_jobs(req: FindJobsRequest = None, user=Depends(get_current_user)):
    from app.db.profile import get_profile
    from modules.platforms.registry import PLATFORMS

    profile = get_profile(user.id)
    requested = req.platforms if (req and req.platforms) else profile.get("platforms", ["remoteok"])

    # Indeed is NOT scraped server-side: it's discovered in-browser by the extension
    # during a campaign (per-user home IP), so the "compliant by design" claim holds —
    # our server never scrapes Indeed. Other platforms' listings are still fetched here.
    SERVER_SCRAPE_SKIP = {"indeed"}
    indeed_requested = "indeed" in requested
    scrapeable = [
        p for p in requested
        if p in PLATFORMS and not PLATFORMS[p].requires_credentials and p not in SERVER_SCRAPE_SKIP
    ]

    platforms = [PLATFORMS[p]() for p in scrapeable]
    all_jobs, searched = [], []

    for platform in platforms:
        found = platform.scrape(
            keywords=profile.get("keywords", []),
            location=profile.get("location", "remote"),
            max_results=25,
        )
        all_jobs.extend(found)
        searched.append(platform.display_name)

    # Helpful note so "Find Jobs" isn't silently empty when only Indeed was requested.
    indeed_note = "Indeed jobs appear once you start a campaign." if indeed_requested else ""

    if not all_jobs:
        msg = f"No jobs found from {', '.join(searched)}" if searched else "No jobs found"
        if indeed_note:
            msg = indeed_note if not searched else f"{msg}. {indeed_note}"
        return {"count": 0, "message": msg, "platforms": searched, "indeed_note": indeed_note}

    # Deduplicate against already-saved jobs, then let Claude score everything.
    # Keyword pre-filter removed: JobSpy platforms already filter via search_term,
    # and Claude Haiku (Haiku cost ~$0.025/200 jobs) is a better semantic judge.
    new_jobs = [j for j in all_jobs if not jobs_db.job_exists(user.id, j["link"])]
    # Salary filter runs BEFORE AI scoring (the whole point: don't spend model tokens or
    # an application slot on out-of-range pay). Unlisted salary passes unless the user
    # opted into listed-only — see modules/salary_filter.py.
    from modules.salary_filter import filter_by_salary
    new_jobs, salary_dropped = filter_by_salary(new_jobs, profile)
    resume_text = None
    if new_jobs:
        from modules.ai_cover_letter import load_resume_text
        from modules.ai_job_scorer import score_jobs_batch

        resume_text = load_resume_text(profile.get("resume_url"))
        new_jobs = score_jobs_batch(new_jobs, profile, resume_text)

    # Resume tailoring is now LAZY (economics #2): it moved out of discovery into
    # the apply-time path — GET /profile/resume/url/best tailors a job on demand the
    # first time the extension fetches its resume to apply. So we pay ~$0.028/tailor
    # only for jobs that reach a real submission, not for every score-≥N job discovered
    # (~70% of which were never applied to). Gating (Premium + Apply-Mode threshold)
    # lives there now. See PLATFORM_AUTOMATION_PLAN.md.

    saved = 0
    for job in new_jobs:
        job_id = jobs_db.save_job(
            user_id=user.id,
            title=job["title"],
            company=job["company"],
            link=job["link"],
            platform=job.get("platform", "unknown"),
            description=job.get("description", ""),
            location=job.get("location", ""),
            job_type=job.get("job_type", ""),
        )
        if job_id and job.get("score") is not None:
            jobs_db.update_job_score(
                job_id,
                user.id,
                job["score"],
                job.get("ai_verdict", ""),
                job.get("ai_flags", []),
                job.get("ats_keywords", []),
                job.get("ats_match_pct", 0),
            )
        saved += 1

    message = f"{saved} new jobs saved"
    if salary_dropped:
        message = f"{message} ({salary_dropped} outside your salary range)"
    if indeed_note:
        message = f"{message}. {indeed_note}"
    return {"count": saved, "message": message, "platforms": searched, "indeed_note": indeed_note,
            "salary_filtered": salary_dropped}


@router.post("/jobs/find-ats")
def find_ats_jobs(user=Depends(get_current_user)):
    """Direct-source discovery from Greenhouse/Lever public board APIs (ROADMAP_E2E.md).

    The real supply of good-fit external-apply jobs: a curated watchlist of company tokens
    queried via their official board APIs → direct, phase_ats-fillable apply URLs. Scores +
    saves into the same job pool as /jobs/find so the campaign + Fit Engine treat them
    uniformly. Compliant (public APIs, server-side, no scraping). Mirrors find_jobs.
    """
    from app.db.profile import get_profile
    from data.ats_watchlist import SEED_WATCHLIST
    from modules.platforms.ats_boards import discover_ats

    # WORKER-STARVATION GUARD (GLOBAL_PLAN "backend worker starvation → 504"): this
    # handler synchronously hits ~46 board APIs (12s timeout each) + AI-scores up to 120
    # jobs — it can hold a worker for minutes. The extension calls it on EVERY campaign
    # start, so restart-loops during testing (or a zombie campaign) stack these and starve
    # the pool → /campaign/status and /campaign/stop hang → dashboard 504s / minute-long
    # Stops. The pool barely changes minute-to-minute, so within the cooldown just serve
    # from the existing pool. In-memory is fine: worst case after a backend restart is one
    # extra discovery sweep.
    now = time.time()
    last = _FIND_ATS_LAST_RUN.get(user.id, 0.0)
    if now - last < FIND_ATS_COOLDOWN_SECS:
        remaining = int(FIND_ATS_COOLDOWN_SECS - (now - last))
        return {
            "count": 0,
            "found": 0,
            "cooldown": True,
            "retry_in_secs": remaining,
            "message": f"ATS discovery ran recently — using the existing job pool (refresh in ~{max(1, remaining // 60)} min)",
        }
    _FIND_ATS_LAST_RUN[user.id] = now

    from modules.ai_cover_letter import load_resume_text

    profile = get_profile(user.id)
    keywords = profile.get("keywords", [])
    resume_text = load_resume_text(profile.get("resume_url"))

    try:
        found = discover_ats(SEED_WATCHLIST, keywords, cap=120)
    except Exception as e:  # never let a flaky company API break discovery
        print(f"[find-ats] discovery failed: {e}", file=sys.stderr)
        found = []

    new_jobs = [j for j in found if not jobs_db.job_exists(user.id, j["link"])]
    # Salary filter BEFORE AI scoring — same early gate as /jobs/find.
    from modules.salary_filter import filter_by_salary
    new_jobs, salary_dropped = filter_by_salary(new_jobs, profile)
    if new_jobs:
        from modules.ai_job_scorer import score_jobs_batch

        new_jobs = score_jobs_batch(new_jobs, profile, resume_text)

    saved = 0
    for job in new_jobs:
        job_id = jobs_db.save_job(
            user_id=user.id,
            title=job["title"],
            company=job["company"],
            link=job["link"],
            platform=job.get("platform", "unknown"),  # "greenhouse" | "lever"
            description=job.get("description", ""),
            location=job.get("location", ""),
            job_type=job.get("job_type", ""),
        )
        if job_id and job.get("score") is not None:
            jobs_db.update_job_score(
                job_id, user.id, job["score"], job.get("ai_verdict", ""),
                job.get("ai_flags", []), job.get("ats_keywords", []),
                job.get("ats_match_pct", 0),
            )
        saved += 1

    # Self-heal: re-score GH/Lever jobs still in the pool with an empty description from
    # before the ?content=true fix, so the deck ranks best-fit-first without a manual
    # trigger. Reuses the descriptions we JUST fetched in `found` (no extra board calls),
    # and self-terminates once the pool is healed. Best-effort — never break discovery.
    try:
        fresh_desc = {f["link"]: f["description"] for f in found if f.get("description") and f.get("link")}
        rescored = _backfill_thin_ats_scores(user.id, profile, resume_text, cap=40, desc_source=fresh_desc)
    except Exception as e:
        print(f"[find-ats] backfill skipped: {e}", file=sys.stderr)
        rescored = 0

    return {
        "count": saved,
        "found": len(found),
        "salary_filtered": salary_dropped,
        "rescored": rescored,
        "message": f"{saved} new Greenhouse/Lever jobs saved (from {len(found)} live listings)"
        + (f", {salary_dropped} outside your salary range" if salary_dropped else "")
        + (f"; re-scored {rescored} older jobs" if rescored else ""),
    }


def _backfill_thin_ats_scores(
    user_id: str, profile: dict, resume_text: str, cap: int = 80,
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
        j for j in jobs_db.get_jobs(user_id)
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
                fresh = fetch_greenhouse(token, None, 200) if platform == "greenhouse" else fetch_lever(token, None, 200)
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
                j["id"], user_id, scored["score"], scored.get("verdict", ""),
                scored.get("flags", []), scored.get("ats_keywords", []), scored.get("ats_match_pct", 0),
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
    from modules.ai_cover_letter import load_resume_text

    profile = get_profile(user.id)
    resume_text = load_resume_text(profile.get("resume_url"))
    rescored = _backfill_thin_ats_scores(user.id, profile, resume_text)
    return {
        "rescored": rescored,
        "message": f"Re-scored {rescored} GH/Lever jobs with real descriptions — the deck now ranks best-fit-first."
        if rescored else "No thin-description GH/Lever jobs to backfill.",
    }


@router.post("/jobs/ingest")
def ingest_jobs(req: IngestJobsRequest, user=Depends(get_current_user)):
    """Harvest-to-pool: the extension saves job cards it SEES in the user's browser
    during a campaign walk (Indeed/ZR search pages). This is the compliant-by-design
    Indeed discovery path — the server never scrapes Indeed (see find_jobs); the
    user's own browser on their home IP does, and the tap deck feeds from this pool.

    INSERT-only: existing links are skipped entirely (an upsert would reset the row's
    status to 'new', resurrecting applied/skipped/approved jobs — the dedup lane).
    No AI scoring here: cards carry no description, and title-only scoring is the
    exact ~6%-noise trap the GH backfill just fixed. score stays null → the deck
    sorts them after scored jobs until a description-bearing pass rescores them.
    """
    HARVEST_PLATFORMS = {"indeed", "ziprecruiter"}
    saved, skipped = 0, 0
    for j in (req.jobs or [])[:30]:  # per-call cap: one search page is ~15 cards
        if j.platform not in HARVEST_PLATFORMS or not j.link or not j.title:
            continue
        if jobs_db.job_exists(user.id, j.link):
            skipped += 1
            continue
        jobs_db.save_job(
            user_id=user.id,
            title=j.title,
            company=j.company,
            link=j.link,
            status="new",
            platform=j.platform,
            description=j.description,
            location=j.location,
            job_type=j.job_type,
        )
        saved += 1
    return {"saved": saved, "skipped_existing": skipped}


@router.patch("/jobs/{job_id}/status")
def patch_job_status(job_id: str, req: JobStatusUpdate, user=Depends(get_current_user)):
    jobs_db.update_job_status(user.id, job_id, req.status)
    return {"updated": True, "job_id": job_id, "status": req.status}


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
        return {"tailored": True, "url": resume_storage.signed_url_from_path(job["tailored_resume_pdf_url"], user.id)}
    return {"tailored": False, "reason": "Not eligible — Premium + strong match required, or no resume uploaded."}
