import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.db import applications as apps_db
from app.db import jobs as jobs_db
from app.db.subscriptions import check_can_apply
from app.deps import get_current_user
from app.schemas import ApplicationSaveRequest

router = APIRouter(tags=["applications"])


@router.post("/applications/save")
def save_application(req: ApplicationSaveRequest, user=Depends(get_current_user)):
    """Сохраняет заявку из Chrome Extension, проверяет лимиты тира."""
    check = check_can_apply(user.id, req.platform, getattr(user, "email", None))
    if not check["allowed"]:
        return JSONResponse(
            status_code=429,
            content={
                "error": "limit_exceeded",
                "message": check["reason"],
                "tier": check["tier"],
                "used_today": check["used_today"],
                "daily_limit": check["daily_limit"],
            },
        )

    job_id = jobs_db.save_job(
        user_id=user.id,
        title=req.job_title,
        company=req.company,
        link=req.job_url,
        status=req.status,
        platform=req.platform,
    )
    apps_db.save_application(
        user_id=user.id,
        job_id=job_id,
        cover_letter=req.cover_letter,
        status=req.status,
    )
    return {
        "saved": True,
        "job_id": job_id,
        "used_today": check["used_today"] + 1,
        "daily_limit": check["daily_limit"],
        "tier": check["tier"],
    }


@router.get("/applications/history")
def applications_history(user=Depends(get_current_user)):
    return apps_db.get_history(user.id)
