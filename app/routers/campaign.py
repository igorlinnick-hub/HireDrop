import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import applications as apps_db
from app.db import campaign as campaign_db
from app.db import jobs as jobs_db
from app.db.client import get_supabase
from app.db.profile import get_profile
from app.db.subscriptions import MAX_PER_PLATFORM, daily_limit, get_submit_mode, get_tier
from app.deps import get_current_user
from app.schemas import CampaignStartRequest

router = APIRouter(tags=["campaign"])

# Single source of truth for the caps lives in app/db/subscriptions.py — the
# extension used to hardcode its own 50 here and in content.js/background.js,
# which diverged from the ban-safety gate (MAX_PER_PLATFORM). Status now serves
# the authoritative numbers so the extension can enforce them PRE-submit (before
# the irreversible application) instead of discovering the 429 after the fact.


@router.get("/campaign/status")
def campaign_status(user=Depends(get_current_user)):
    state = campaign_db.get_state(user.id)
    profile = get_profile(user.id)
    enabled_platforms = profile.get("platforms", [])

    today_count = apps_db.count_today(user.id)
    platform_counts = apps_db.count_today_by_platform(user.id)
    jobs_ready = jobs_db.count_new_jobs(user.id, enabled_platforms)

    tier = get_tier(user.id, getattr(user, "email", None))
    submit_mode = get_submit_mode(user.id)

    return {
        "running": state["running"],
        "filters": state["filters"],
        "started_at": state["started_at"],
        "today_applications": today_count,
        "platform_counts": platform_counts,
        # Two-cap model: per-platform ceiling (ban-safety rail, counted per platform)
        # + total daily budget (tier value/cost, raised in tap mode). Extension enforces BOTH pre-submit.
        "limit_per_platform": MAX_PER_PLATFORM,
        "daily_limit": daily_limit(tier, submit_mode),
        "tier": tier,
        "submit_mode": submit_mode,
        "jobs_ready": jobs_ready,
    }


@router.post("/campaign/start")
def campaign_start(req: CampaignStartRequest, user=Depends(get_current_user)):
    # Fail-closed onboarding gate (defense-in-depth: the dashboard layout and
    # the extension's START_CAMPAIGN both check too). An un-onboarded profile
    # has no name/keywords/resume — a campaign would file applications with
    # blanks under the user's identity.
    profile = get_profile(user.id)
    if not profile.get("onboarding_completed"):
        raise HTTPException(status_code=403, detail="onboarding_incomplete")
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
    try:
        get_supabase().table("campaign_screenshots").delete().eq("user_id", user.id).execute()
    except Exception:
        pass
    return {"stopped": True}


class ExtensionPingBody(BaseModel):
    campaign_running: bool = False
    today_count: int = 0
    window_visible: bool = False
    last_screenshot_age: float | None = None
    error: str | None = None
    version: str | None = None


_ext_status: dict = {}  # user_id -> {ts, campaign_running, ...}


@router.post("/extension/ping")
def extension_ping(body: ExtensionPingBody, user=Depends(get_current_user)):
    _ext_status[user.id] = {
        "ts": time.time(),
        "campaign_running": body.campaign_running,
        "today_count": body.today_count,
        "window_visible": body.window_visible,
        "last_screenshot_age": body.last_screenshot_age,
        "error": body.error,
        "version": body.version,
    }
    # Return the backend's authoritative campaign flag so the extension can honor a Stop
    # even if the dashboard's postMessage stop was dropped (e.g. orphaned content script).
    try:
        should_run = bool(campaign_db.get_state(user.id)["running"])
    except Exception:
        should_run = True  # fail-open: never stop a campaign on a state-read hiccup
    return {"ok": True, "should_run": should_run}


@router.get("/extension/ping")
def extension_status(user=Depends(get_current_user)):
    row = _ext_status.get(user.id)
    if not row:
        return {"online": False}
    age = time.time() - row["ts"]
    return {
        "online": age < 60,
        "last_seen_secs_ago": round(age),
        **{k: v for k, v in row.items() if k != "ts"},
    }


class ScreenshotBody(BaseModel):
    screenshot: str


@router.post("/campaign/screenshot")
def upload_screenshot(body: ScreenshotBody, user=Depends(get_current_user)):
    try:
        get_supabase().table("campaign_screenshots").upsert(
            {"user_id": user.id, "data": body.screenshot, "ts": time.time()},
            on_conflict="user_id",
        ).execute()
    except Exception:
        pass  # non-blocking — missed frame is fine
    return {"ok": True}


@router.get("/campaign/screenshot")
def get_screenshot(user=Depends(get_current_user)):
    try:
        res = get_supabase().table("campaign_screenshots").select("data,ts").eq("user_id", user.id).execute()
    except Exception:
        return {"data": None}
    if not res.data:
        return {"data": None}
    row = res.data[0]
    if time.time() - row["ts"] > 10:
        return {"data": None}
    return {"data": row["data"]}
