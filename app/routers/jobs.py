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
    if new_jobs:
        from modules.ai_cover_letter import load_resume_text
        from modules.ai_job_scorer import score_jobs_batch

        resume_text = load_resume_text(profile.get("resume_url"))
        new_jobs = score_jobs_batch(new_jobs, profile, resume_text)

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
        # Tailor resume for strong matches (score 7+)
        if job_id and job.get("score", 0) >= 7 and resume_text:
            from modules.ai_resume_tailor import tailor_resume
            tailored = tailor_resume(job, profile, resume_text)
            if tailored:
                jobs_db.update_tailored_resume(job_id, tailored)
                # Generate per-job ATS PDF from tailored text
                try:
                    from modules.ats_pdf_generator import generate_ats_pdf
                    from app.db import resume as resume_storage
                    pdf_bytes = generate_ats_pdf(resume_text=tailored)
                    pdf_path = resume_storage.upload_job_tailored(user.id, job_id, pdf_bytes)
                    jobs_db.update_tailored_resume_pdf(job_id, pdf_path)
                except Exception as pdf_err:
                    print(f"[jobs] per-job PDF skipped: {pdf_err}")
        saved += 1

    message = f"{saved} new jobs saved"
    if indeed_note:
        message = f"{message}. {indeed_note}"
    return {"count": saved, "message": message, "platforms": searched, "indeed_note": indeed_note}


@router.patch("/jobs/{job_id}/status")
def patch_job_status(job_id: str, req: JobStatusUpdate, user=Depends(get_current_user)):
    jobs_db.update_job_status(user.id, job_id, req.status)
    return {"updated": True, "job_id": job_id, "status": req.status}
