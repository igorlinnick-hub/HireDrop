import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends

from app.db import applications as apps_db
from app.db import campaign as campaign_db
from app.db import jobs as jobs_db
from app.db.profile import get_profile
from app.deps import get_current_user
from app.schemas import CampaignStartRequest

router = APIRouter(tags=["campaign"])

LIMIT_PER_PLATFORM = 50


@router.get("/campaign/status")
def campaign_status(user=Depends(get_current_user)):
    state = campaign_db.get_state(user.id)
    profile = get_profile(user.id)
    enabled_platforms = profile.get("platforms", [])

    today_count = apps_db.count_today(user.id)
    platform_counts = apps_db.count_today_by_platform(user.id)
    jobs_ready = jobs_db.count_new_jobs(user.id, enabled_platforms)

    return {
        "running": state["running"],
        "filters": state["filters"],
        "started_at": state["started_at"],
        "today_applications": today_count,
        "platform_counts": platform_counts,
        "limit_per_platform": LIMIT_PER_PLATFORM,
        "jobs_ready": jobs_ready,
    }


@router.post("/campaign/start")
def campaign_start(req: CampaignStartRequest, user=Depends(get_current_user)):
    filters = {
        "keywords": req.keywords,
        "platforms": req.platforms,
        "location": req.location,
        "job_type": req.job_type,
    }
    state = campaign_db.start(user.id, filters)
    return {"started": True, "state": state}


@router.post("/campaign/stop")
def campaign_stop(user=Depends(get_current_user)):
    campaign_db.stop(user.id)
    return {"stopped": True}
