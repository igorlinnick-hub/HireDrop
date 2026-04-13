import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app.deps import get_current_user
from app.schemas import FindJobsRequest, JobStatusUpdate
from database.db import (
    get_all_jobs, save_job, update_job_status, job_exists, get_connection
)
from modules.platforms import get_enabled_platforms
from modules.filters import filter_jobs
from modules.telegram_bot import send_notification

router = APIRouter(tags=["jobs"])

CONNECTABLE_PLATFORMS = ["indeed", "wellfound"]
STUB_PLATFORMS = ["glassdoor", "ziprecruiter"]


@router.get("/jobs")
def get_jobs(user=Depends(get_current_user)):
    jobs = get_all_jobs()
    return [
        {
            "id": job["id"],
            "title": job["title"],
            "company": job["company"],
            "link": job["link"],
            "status": job["status"],
            "date_found": job["date_found"],
            "platform": job["platform"] if "platform" in job.keys() else "remoteok",
        }
        for job in jobs
    ]


@router.post("/jobs/find")
def find_jobs(req: FindJobsRequest = None, user=Depends(get_current_user)):
    from data_helpers import load_profile, load_connections
    from modules.platforms.registry import PLATFORMS

    profile = load_profile()
    conns = load_connections()
    requested = req.platforms if (req and req.platforms) else profile.get("platforms", ["remoteok"])

    scrapeable = []
    for p in requested:
        if p == "remoteok":
            scrapeable.append(p)
        elif p in CONNECTABLE_PLATFORMS and conns.get(p, {}).get("connected"):
            scrapeable.append(p)
        elif p in STUB_PLATFORMS:
            pass  # skip silently

    platforms = [PLATFORMS[p]() for p in scrapeable if p in PLATFORMS]
    all_jobs = []
    searched_platforms = []
    for platform in platforms:
        jobs = platform.scrape(
            keywords=profile.get("keywords", []),
            location=profile.get("location", "remote"),
            max_results=25,
        )
        all_jobs.extend(jobs)
        searched_platforms.append(platform.display_name)

    if not all_jobs:
        return {"count": 0, "message": f"No jobs found from {', '.join(searched_platforms)}", "platforms": searched_platforms}

    filtered = filter_jobs(all_jobs)
    if not filtered:
        return {"count": 0, "message": "No new jobs matching keywords", "platforms": searched_platforms}

    for job in filtered:
        save_job(
            title=job["title"],
            company=job["company"],
            link=job["link"],
            platform=job.get("platform", "unknown"),
            description=job.get("description", ""),
            location=job.get("location", ""),
            job_type=job.get("job_type", ""),
        )

    send_notification(f"JobFlow: {len(filtered)} new jobs found on {', '.join(searched_platforms)}!")
    return {"count": len(filtered), "message": f"{len(filtered)} new jobs saved", "platforms": searched_platforms}


@router.patch("/jobs/{job_id}/status")
def patch_job_status(job_id: int, req: JobStatusUpdate, user=Depends(get_current_user)):
    update_job_status(job_id, req.status)
    return {"updated": True, "job_id": job_id, "status": req.status}
