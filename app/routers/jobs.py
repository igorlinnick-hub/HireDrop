import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends

from app.db import jobs as jobs_db
from app.deps import get_current_user
from app.schemas import FindJobsRequest, JobStatusUpdate
router = APIRouter(tags=["jobs"])

@router.get("/jobs")
def get_jobs(user=Depends(get_current_user)):
    return jobs_db.get_jobs(user.id)


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
                job["score"],
                job.get("ai_verdict", ""),
                job.get("ai_flags", []),
                job.get("ats_keywords", []),
                job.get("ats_match_pct", 0),
            )
        saved += 1

    message = f"{saved} new jobs saved"
    if indeed_note:
        message = f"{message}. {indeed_note}"
    return {"count": saved, "message": message, "platforms": searched, "indeed_note": indeed_note}


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
        return {"tailored": True, "url": resume_storage.signed_url_from_path(job["tailored_resume_pdf_url"])}
    return {"tailored": False, "reason": "Not eligible — Premium + strong match required, or no resume uploaded."}
