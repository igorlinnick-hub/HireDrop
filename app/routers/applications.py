import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends
from datetime import datetime

from app.deps import get_current_user
from app.schemas import ApplicationSaveRequest
from database.db import save_job, save_application, update_job_status, get_connection

router = APIRouter(tags=["applications"])


@router.post("/applications/save")
def save_application_endpoint(req: ApplicationSaveRequest, user=Depends(get_current_user)):
    """Save a completed application (called from Chrome Extension)."""
    job_id = save_job(
        title=req.job_title,
        company=req.company,
        link=req.job_url,
        status=req.status,
        platform=req.platform,
    )
    save_application(job_id, cover_letter=req.cover_letter)
    update_job_status(job_id, req.status)
    return {"saved": True, "job_id": job_id}


@router.get("/applications/history")
def applications_history(user=Depends(get_current_user)):
    """Return last 50 applications with job details."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT jobs.title, jobs.company, jobs.platform, jobs.link,
               applications.date_applied, applications.status, applications.cover_letter
        FROM applications
        JOIN jobs ON applications.job_id = jobs.id
        ORDER BY applications.date_applied DESC
        LIMIT 50
    """)
    rows = [
        {
            "title": row["title"],
            "company": row["company"],
            "platform": row["platform"],
            "link": row["link"],
            "date_applied": row["date_applied"],
            "status": row["status"],
            "cover_letter": row["cover_letter"],
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    return rows
