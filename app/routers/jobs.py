import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends

from app.db import jobs as jobs_db
from app.deps import get_current_user
from app.schemas import FindJobsRequest, JobStatusUpdate
from modules.filters import filter_jobs
from modules.telegram_bot import send_notification

router = APIRouter(tags=["jobs"])

CONNECTABLE_PLATFORMS = ["indeed", "wellfound"]
STUB_PLATFORMS = ["glassdoor", "ziprecruiter"]


@router.get("/jobs")
def get_jobs(user=Depends(get_current_user)):
    return jobs_db.get_jobs(user.id)


@router.post("/jobs/find")
def find_jobs(req: FindJobsRequest = None, user=Depends(get_current_user)):
    from app.db.profile import get_connections, get_profile
    from modules.platforms.registry import PLATFORMS

    profile = get_profile(user.id)
    conns = get_connections(user.id)
    requested = req.platforms if (req and req.platforms) else profile.get("platforms", ["remoteok"])

    scrapeable = [
        p
        for p in requested
        if p == "remoteok" or (p in CONNECTABLE_PLATFORMS and conns.get(p, {}).get("connected"))
    ]

    platforms = [PLATFORMS[p]() for p in scrapeable if p in PLATFORMS]
    all_jobs, searched = [], []

    for platform in platforms:
        found = platform.scrape(
            keywords=profile.get("keywords", []),
            location=profile.get("location", "remote"),
            max_results=25,
        )
        all_jobs.extend(found)
        searched.append(platform.display_name)

    if not all_jobs:
        return {
            "count": 0,
            "message": f"No jobs found from {', '.join(searched)}",
            "platforms": searched,
        }

    filtered = filter_jobs(all_jobs, profile)
    if not filtered:
        return {"count": 0, "message": "No new jobs matching keywords", "platforms": searched}

    saved = 0
    for job in filtered:
        if not jobs_db.job_exists(user.id, job["link"]):
            jobs_db.save_job(
                user_id=user.id,
                title=job["title"],
                company=job["company"],
                link=job["link"],
                platform=job.get("platform", "unknown"),
                description=job.get("description", ""),
                location=job.get("location", ""),
                job_type=job.get("job_type", ""),
            )
            saved += 1

    send_notification(f"JobFlow: {saved} new jobs found on {', '.join(searched)}!")
    return {"count": saved, "message": f"{saved} new jobs saved", "platforms": searched}


@router.patch("/jobs/{job_id}/status")
def patch_job_status(job_id: str, req: JobStatusUpdate, user=Depends(get_current_user)):
    jobs_db.update_job_status(user.id, job_id, req.status)
    return {"updated": True, "job_id": job_id, "status": req.status}
